import click
import re

import asyncio
import aiohttp

from itertools import product

import time

import random

class Charset:
    PIECE_PATTERN = re.compile(r'^(?:(.)-(.))|(.+)$')

    def __init__(self, *pieces):
        self.pieces = []
        self.sizes = []

        for piece in pieces:
            match = Charset.PIECE_PATTERN.match(piece)

            if match is None:
                # If for some reason it doesn't match raise an error
                raise ValueError(f'Invalid charset piece {piece}')

            if match.group(3) is not None:
                # Piece is a block of characters to be taken literally
                self.pieces.append((match.group(3),))
                self.sizes.append(1)
            else:
                # Piece is a range between two characters
                char1, char2 = match.group(1), match.group(2)

                if char1 == char2:
                    # Characters are the same, so really it is a block of 1 character
                    self.pieces.append((char1,))
                    self.sizes.append(1)
                else:
                    # Piece is actually a range

                    if ord(char1) > ord(char2):
                        # If the characters are out of order
                        char1, char2 = char2, char1

                    self.pieces.append((char1, char2))
                    self.sizes.append(ord(char2) - ord(char1))
        
        # Lock pieces and sizes in
        self.pieces = tuple(self.pieces)
        self.sizes = tuple(self.sizes)
    
    def __str__(self):
        # Create a readable format using , as separator and no escape characters
        return ','.join('-'.join(piece) for piece in self.pieces)
    
    def __len__(self):
        """Length of the Charset, blocks of characters are counted as 1"""
        return sum(self.sizes)
    
    def __iter__(self):
        """Iterates over the pieces with no state storage in the Charset instance and no duplicating the pieces in memory"""
        for piece in self.pieces:
            if len(piece) == 1:
                # Piece is a single character/set of characters
                yield piece[0]
            else:
                # Iterate through each codepoint between char1 and char2
                for point in range(ord(piece[0]), ord(piece[1])):
                    yield chr(point)

    def __getitem__(self, index):
        """Returns the character at index index"""
        # TODO: add support for splicing possibly to allow for more complex control schemes for pure brute forced attacks

        if index < 0:
            # Account for negative indices
            index = len(self) - index
        
        for piece, size in zip(self.pieces, self.sizes):
            if index >= size:
                # char is not in this piece
                index -= size
            else:
                # For single blocks of characters
                if size == 1:
                    return piece[0]
                else:
                    # Get char at remaining index + offset of first char in block
                    return chr(ord(piece[0]) + index)


class CharsetType(click.ParamType):
    name = 'Charset'

    def __init__(self, split, escape):
        super().__init__()

        if len(split) > 1 or len(escape) > 1:
            # Ensure split and escape are single characters
            raise ValueError('split and escape must be single characters')

        if split == escape:
            # Ensure escape and split are different
            raise ValueError('split and escape must be different characters')

        self.split = split
        self.escape = escape

    def splitByExcaped(self, string):
        """Splits the string by split, escaping any characters with escape"""
        
        pieces = []
        current = ''
        escaped = False


        for c in string:
            if escaped:
                # Check if this character has been escaped, if so add it directly to the
                current += c
                escaped = False
            elif c == self.escape:
                # Next check if the character is the escape
                escaped = True
            elif c == self.split:
                # Next check if the character is the split, if so, add the current piece to pieces and start a new piece
                # Only add this piece if it contains characters
                if current:
                    pieces.append(current)
                    current = ''
            else:
                # Otherwise add the character as is
                current += c
        
        if current:
            # Add the last piece, if it contains characters
            pieces.append(current)
        
        if escaped:
            # Check for escape character escaping nothing
            raise ValueError('Invalid Charset')
        
        return pieces


    def convert(self, value, param, ctx):
        if type(value) != str:
            # If value has been converted or None pass through
            return value
        
        charset = Charset(*self.splitByExcaped(value))

        if len(charset) == 0:
            # Ensure atleast 1 character was specified
            raise ValueError('Charset has no valid characters')

        return charset


class CharsetGenerator:
    def __init__(self, chars, mi, ma):
        self.chars = chars
        self.mi = mi
        self.ma = ma
    
    def __iter__(self):
        for l in range(self.mi, self.ma + 1):
            for p in product(self.chars, repeat=l):
                yield ''.join(p)


class HBusterSession:
    def __init__(self, tasks, dirlist, chars, min, max, recursive, extensions, validStatus, host):
        self.numTasks = tasks
        self.dirlist = dirlist
        self.chars = chars
        self.minLength = min
        self.maxLength = max
        self.recursive = recursive
        self.extensions = extensions
        self.validStatus = set(validStatus.split(','))

        self.host = host

        self.listBased = dirlist is not None

        # Only used if non list based
        self.charGenerator = CharsetGenerator(self.chars, self.minLength, self.maxLength)

        # Directories currently being explored
        self.workPools = ['']

        # "Files" to pull from for each directory
        self.poolFiles = []
        self.addFile()

        # Just to safeguard against repeated directories in the list
        self.seen = set()
        self.seen.add('')

        # A list of valid urls
        self.found = []

        # Time keeping and rate tracking
        self.startTime = None
        self.requests = 0
        self.requestRate = 0
        self.running = False
    
    def addFile(self):
        if self.listBased:
            self.poolFiles.append(open(self.dirlist, 'r'))
        else:
            self.poolFiles.append(iter(self.charGenerator))

    def close(self):
        if self.listBased:
            for f in self.poolFiles:
                f.close()
    
    async def timekeeper(self):
        self.startTime = time.perf_counter()

        lastTime = time.perf_counter()
        lastRequests = self.requests

        self.running = True

        while self.running:
            await asyncio.sleep(1)

            deltaTime = time.perf_counter() - lastTime
            deltaRequests = self.requests - lastRequests

            self.requestRate = deltaRequests / deltaTime

            print(self.requestRate)

            lastTime = time.perf_counter()
            lastRequests = self.requests

    
    async def testPath(self, path):
        url = self.host + path

        async with self.session.get(url) as resp:
            if (await resp.status()) in self.validStatus:
                self.found.append(url)
                return True
        
        return False
    
    async def task(self, id):
        while self.workPools:
            # Figure out what this task is working on
            currentJob = id % len(self.workPools)

            confirmed = self.workPools[currentJob]
            try:
                extension = next(self.poolFiles[currentJob]).strip()

                path = confirmed + '/' + extension

                if path not in self.seen:
                    for fileExt in self.extensions:
                        if await self.testPath(path + fileExt):
                            # Path is valid
                            click.echo(path)
                            if self.recursive:
                                self.workPools.append(path)
                                self.addFile()
                                self.seen.add(path)
                        self.requests += 1

            except StopIteration:
                self.workPools.pop(currentJob)
                self.poolFiles.pop(currentJob)
    
    async def start(self):
        taskPool = [self.task(i) for i in range(self.numTasks)]

        timekeeper = asyncio.create_task(self.timekeeper())

        async with aiohttp.ClientSession() as self.session:
            await asyncio.gather(*taskPool)
        
        # Stop timekeeper
        self.running = False
        
        click.echo(self.found)



@click.command(help='Asynchronous directory brute forcer written in Python 3.')
@click.option('--tasks', '-t', default=1, help='number of tasks to use', type=click.IntRange(min=1), show_default=True)
@click.option('--dirlist', '-d', default=None, help='word list file to read from, if not provided a pure brute force method is used', type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option('--chars', '-c', default='a-z,A-Z,0-9,%20,-,_', help='character set to pull from if using a pute brute force method', type=CharsetType(',', '\\'), show_default=True)
@click.option('--min', '-m', default=1, help='minimum number of characters to use in dirnames if using pure brute force method', type=click.IntRange(min=1), show_default=True)
@click.option('--max', '-M', default=8, help='maximum number of characters to use in dirnames if using pure brute force method', type=click.IntRange(min=1), show_default=True)
@click.option('--recursive', '-r', default=False, help='recursively explore found directories', is_flag=True)
@click.option('--extensions', '-e', default=None, help='file extensions to try', show_default=True)
@click.option('--status', '-s', default='200', help='status codes that mark a directory as valid, comma separated, can lead to false positives')
@click.argument('host')
def hbuster(tasks, dirlist, chars, min, max, recursive, extensions, status, host):
    if dirlist and min > max:
        # Abort if min and max are invalid, but only if using a pure brute force method
        raise click.BadParameter('min must be less than max', param_hint=['min','max'])

    # Add null extension to extensions and format
    extensions = ['']
    if extensions is not None:
        extensions += [f'.{ext}' for ext in extensions]

    if host[-1] == '/':
        # Remove trailing /
        host = host[:-1]
    
    session = HBusterSession(tasks, dirlist, chars, min, max, recursive, extensions, status, host)

    # Start attack
    loop = asyncio.get_event_loop()
    T = time.perf_counter()
    try:
        loop.run_until_complete(session.start())
    finally:
        session.close()
        loop.close()
    
    T = time.perf_counter() - T

    click.echo(f'Done in {T:.2f}s')

