from urllib.parse import unquote
import asyncio
import traceback
import logging
import sys
import json
import time
import re

# Initialization
# Typical CCSO port is 105 (as for S/Gopher, no thank you)
port = 105
# how frequently reload can be used to reload the database (in seconds)
reload_cooldown = 60
encoding = 'ascii'
newline = '\r\n'

unique_fields = []
database = []
server_status = []
siteinfo = []
last_reload = 0

# Field setup
# These are our field choices--you can set your own as necessary
# These are fields that always get returned regardless of query
always_fields = ['name']

# Fields that are labeled as indexable (you'll need at least one to be
# able to do searches in some if not all clients)
search_fields = ['name', 'species', 'affiliation', 'universe', 'type',
'creator']

# Fields you can choose to specifically only see when doing a query
filterable_fields = ['name', 'sex', 'species', 'affiliation', 'universe',
'site', 'email', 'discord', 'age', 'summary', 'projects', 'type',
'location', 'creator']

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s: %(message)s',
    handlers=[
        logging.FileHandler('ccso.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('logger')

# For sending lines to the client that will need a newline afterwards
def nl(x=''):
    return str(x) + newline

# Encoding strings into bytes
def to_bytes(x):
    if isinstance(x, list):
        return bytes(newline.join(x), encoding)
    else:
        return bytes(str(x), encoding)

# Reload all files
def reload_db():
    global database
    global server_status
    global siteinfo
    with open('entries.json', 'r') as d:
        database = json.load(d)
        logger.info('Database read from entries.json')
    with open('status.txt', 'r') as u:
        for line in u:
            server_status.append(line.rstrip('\n'))
        server_status.append(nl('201:Database ready, read-only.'))
        logger.info('Server status read from status.txt')
    with open('siteinfo.txt', 'r') as i:
        for line in i:
            siteinfo.append(line.rstrip('\n'))
        siteinfo.append(nl('200:Ok.'))
        logger.info('Siteinfo read from siteinfo.txt')

def find_all_fields():
    global unique_fields

    for entry in database:
        for field in entry:
            if not field in unique_fields:
                unique_fields.append(field)

logger.info('SomnolCCSO v0.3 started')

# Read database for first boot
reload_db()

class PhProtocol(asyncio.Protocol):
    # Start the connection, print to console who dis
    def connection_made(self, transport):
        self.transport = transport
        logger.info('Connected by ' + str(transport.get_extra_info('peername')))

    def data_received(self, data):
        global last_reload

        request = data.decode('utf-8')

        # Lots of command scrubbing
        # Mosaic likes to do percent encoding, seen other clients add extra line breaks to their commands
        request = unquote(request)
        request = request.replace('ph ', '')
        request = request.strip('\r\n/')

        # spits the raw request out into the console for debugging
        logger.info('Client: %s', request)
        commands = request.split('\r\n')

        # All implemented CCSO commands:
        for cmd in commands:
            args = cmd.split(' ')
            try:
                if args[0] == 'status':
                    # reads server status from status.txt
                    resp = to_bytes(server_status)
                    self.transport.write(resp)
                elif args[0] == 'siteinfo':
                    # reads server information from siteinfo.txt
                    resp = to_bytes(siteinfo)
                    self.transport.write(resp)
                elif args[0] == 'reload':
                    if (last_reload + 60) <= time.time():
                        reload_db()
                        last_reload = time.time()
                        self.transport.write(to_bytes(nl('200:Database successfully reloaded.')))
                        logger.info('Database successfully reloaded')
                    else:
                        self.transport.write(to_bytes(nl('520:Please wait ' + str(int((last_reload + reload_cooldown) - time.time())) + ' seconds to reload')))
                        logger.warning('Client tried to reload database too quickly!')
                elif args[0] == 'fields':
                    find_all_fields()
                    results = []
                    keywords = ''
                    _id = 0

                    # Adding keywords onto fields if they're found in the lists at the start
                    for field in unique_fields:
                        _id += 1
                        if field in search_fields:
                            keywords += 'Indexed Lookup '
                        if field in always_fields:
                            keywords += 'Always '
                        if field in filterable_fields:
                            keywords += 'Default'
                        results.append('-200:' + str(_id) + ':' + field + 'max 64 ' + keywords)
                        results.append('-200:' + str(_id) + ':' + field + ': ' + field.title())
                        keywords = ''

                    # Acknowledgement that the command finished regardless of result
                    results.append(nl('200:Ok.'))

                    resp = to_bytes(results)
                    self.transport.write(resp)
                    if logger.isEnabledFor(logging.DEBUG):
                        for i in results:
                            logger.debug(i)
                elif args[0] == 'query':
                    # If the user didn't specify any fields to return, return all attached to matched entries
                    if not 'return' in cmd:
                        cmd += ' return all'

                    criteria = {}
                    matches = ''

                    # If this isn't a syntactically-valid query, error out
                    if re.search(r'(\S*)="([^"]*)"', cmd) is None:
                        self.transport.write(to_bytes(nl('512:Illegal value. All queries need to be formed as [field]="[search]", ie universe="pennyverse".')))
                        logger.warning('Client tried to send illegal query')
                    else:
                        # If this is a syntactically-valid query, go ahead and split it for processing
                        matches = re.finditer(r'(\S*)="([^"]*)"', cmd)

                        for match in matches:
                            criteria[match.group(1)] = match.group(2)

                        # Check to make sure this field actually exists please
                        find_all_fields()

                        if not (list(criteria.keys())[0]) in unique_fields:
                            self.transport.write(to_bytes(nl('507:Field does not exist.')))
                            logger.warning('Desired field does not exist')
                        else:
                            _all = False
                            return_fields = []

                            # The if protects against an edge case where the user types "return" but specifies no fields
                            if re.match(r'.* return', cmd) and not re.match(r'.* return (.*)', cmd):
                                return_fields.append('all')
                            # Splits the list of fields to return into its own list for later checking
                            else:
                                return_fields = re.match(r'.* return (.*)', cmd).group(1).split(' ')
                                return_fields = always_fields + return_fields

                            if 'all' in return_fields:
                                _all = True
                                return_fields.remove('all')

                            results = []
                            entry = 0
                            index = 0

                            # These are to provide better error handling later on
                            # Before, we were reliant on else statements for error handling and those didn't work across an entire dataset, just one entry
                            found_one_match = False
                            found_field = False

                            # Now we run through each item in entries.json to find matches
                            for item in database:
                                entry += 1
                                # Defaults to "do not return" to avoid being able to dump the entire database
                                # Which was possible with the first version :marf:
                                meets_criteria = False
                                for key in criteria:
                                    if key in item:
                                        if not criteria[key].lower() in item[key].lower():
                                            meets_criteria = False
                                            break
                                        else:
                                            index = index + 1
                                            meets_criteria = True
                                            found_one_match = True
                                    else:
                                        meets_criteria = False
                                        break
                                if meets_criteria:
                                    if _all:
                                            for field in list(database[entry - 1]):
                                                found_field = True
                                                # We return a separate index number that counts up by one and NOT the number of the entry in the database
                                                # Netscape/Mosaic don't recognize the start of entries correctly if your index numbers aren't sequential, and phwin truncates fields for the same reason
                                                # Be careful if you're writing your own CCSO server please, this sucked to fix
                                                results.append('-200:' + str(index) + ': ' + field + ': ' + database[entry - 1][field])
                                    else:
                                        for field in return_fields:
                                            if (field in list(database[entry - 1])) and (field in filterable_fields) or _all:
                                                found_field = True
                                                results.append('-200:' + str(index) + ': ' + field + ': ' + database[entry - 1][field])

                            if not found_one_match:
                                # If you found nothing, error out
                                results.append(nl('502:No matches to query.'))
                                logger.warning('Found no matches to client\'s query')
                            elif not found_field:
                                # We found something, just not what the user wanted
                                results.append(nl('508:Field is not present in requested entries.'))
                                logger.warning('Desired field is not present in the entries found')

                            # Acknowledgement that the command finished regardless of result
                            results.append(nl('200:Ok.'))

                            if logger.isEnabledFor(logging.DEBUG):
                                for r in results:
                                    logger.debug(r)

                            resp = to_bytes(results)
                            self.transport.write(resp)
                # If the user inputs a quit command, terminate the connection
                elif args[0] in ['quit', 'stop', 'exit']:
                    self.transport.write(to_bytes(nl('200:Bye!')))
                    self.transport.close()
                    logging.info('Client has disconnected')
                    break
                # This is sent by the PH client after establishing connection
                elif args[0] == 'id':
                    self.transport.write(to_bytes(nl('200:Ok.')))
                # If you try to put in anything other than status, fields, reload, or query
                elif args[0] != '':
                    self.transport.write(to_bytes(nl('514:Unknown command.')))
                    logging.warning('Client sent unknown command')
            # Any generic errors get caught and return 400
            except Exception:
                self.transport.write(to_bytes(nl('400:Server error occurred. That gets a yikes from me.')))
                logging.exception('Exception raised')

async def main(h, p):
    loop = asyncio.get_running_loop()
    server = await loop.create_server(PhProtocol, h, p)
    await server.serve_forever()

logging.info('Server is now running')
asyncio.run(main('0.0.0.0', port))
