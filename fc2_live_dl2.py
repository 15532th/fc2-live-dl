#!/usr/bin/env python3

from datetime import datetime
import argparse
import asyncio
import aiohttp
import signal
import time
import json
import sys

ABOUT = {
    'name': 'fc2-live-dl',
    'version': '1.0.0',
    'date': '2021-08-09',
    'description': 'Download fc2 livestreams',
    'author': 'hizkifw',
    'license': 'MIT',
    'url': 'https://github.com/hizkifw/fc2-live-dl'
}

def clearline():
    print('\033[2K\r', end='')

def debug_log(src, msg):
    print('[{}] {}'.format(src, msg))

def sanitize_filename(fname):
    fname = str(fname)
    for c in '<>:"/\\|?*':
        fname = fname.replace(c, '_')
    return fname

class Logger():
    LOGLEVEL_NONE = 0
    LOGLEVEL_ERROR = 1
    LOGLEVEL_WARN = 2
    LOGLEVEL_INFO = 3
    LOGLEVEL_DEBUG = 4
    LOGLEVEL_TRACE = 5

    loglevel = LOGLEVEL_INFO

    def __init__(self, module):
        self._module = module
        self._loadspin_n = 0

    def trace(self, *args, **kwargs):
        if self.loglevel >= self.LOGLEVEL_TRACE:
            self._print('\033[35m', *args, **kwargs)

    def debug(self, *args, **kwargs):
        if self.loglevel >= self.LOGLEVEL_DEBUG:
            self._print('\033[36m', *args, **kwargs)

    def info(self, *args, **kwargs):
        if self.loglevel >= self.LOGLEVEL_INFO:
            self._print('', *args, **kwargs)

    def warn(self, *args, **kwargs):
        if self.loglevel >= self.LOGLEVEL_WARN:
            self._print('\033[33m', *args, **kwargs)

    def error(self, *args, **kwargs):
        if self.loglevel >= self.LOGLEVEL_ERROR:
            self._print('\033[31m', *args, **kwargs)

    def infol(self, *args):
        if self.loglevel >= self.LOGLEVEL_INFO:
            self._print('\033[2K', *args, end='\r')

    def endl(self):
        if self.loglevel >= self.LOGLEVEL_INFO:
            print('')

    def infospin(self, *args):
        chars = '⡆⠇⠋⠙⠸⢰⣠⣄'
        self._loadspin_n = (self._loadspin_n + 1) % len(chars)
        self.infol(chars[self._loadspin_n], *args)

    def _print(self, prefix, *args, **kwargs):
        print(prefix + '[{}]'.format(self._module), *args, '\033[0m', **kwargs)

class FC2WebSocket():
    heartbeat_interval = 30

    def __init__(self, session, url):
        self._session = session
        self._url = url
        self._msg_id = 0
        self._msg_responses = {}
        self._is_ready = False
        self._logger = Logger('ws')
        self.comments = asyncio.Queue()

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._ws = await self._session.ws_connect(self._url)
        self._logger.debug('connected')
        coros = [self._handle_incoming, self._handle_heartbeat]
        self._tasks = [self._loop.create_task(coro()) for coro in coros]
        return self

    async def __aexit__(self, *err):
        for task in self._tasks:
            task.cancel()
        await self._ws.close()
        self._logger.debug('closed')

    async def get_hls_information(self):
        msg = await self._send_message_and_wait('get_hls_information')
        return msg['arguments']

    async def _handle_incoming(self):
        while True:
            msg = await self._receive_message()
            if msg['name'] == 'connect_complete':
                self._is_ready = True
            elif msg['name'] == '_response_':
                self._msg_responses[msg['id']] = msg['arguments']
            elif msg['name'] == 'control_disconnection':
                code = msg['arguments']['code']
                if code == 4101:
                    raise self.PaidProgramDisconnection()
                elif code == 4512:
                    raise self.MultipleConnectionError()
            elif msg['name'] == 'comment':
                for comment in msg['arguments']['comments']:
                    self.comments.put(comment)

    async def _handle_heartbeat(self):
        while True:
            self._logger.debug('heartbeat')
            await self._send_message('heartbeat')
            await asyncio.sleep(self.heartbeat_interval)

    async def _send_message_and_wait(self, name, arguments={}):
        msg_id = await self._send_message(name, arguments)
        return await self._receive_message(msg_id)

    async def _receive_message(self, msg_id=None):
        while True:
            if msg_id is not None and msg_id in self._msg_responses:
                return self._msg_responses.pop(msg_id)

            msg = await self._ws.receive_json()
            self._logger.trace('<', msg)
            if msg['name'] == '_response_':
                self._msg_responses[msg['id']] = msg
            elif msg_id is None:
                return msg

    async def _send_message(self, name, arguments={}):
        self._msg_id += 1
        self._logger.trace('>', name, arguments)
        await self._ws.send_json({
            'name': name,
            'arguments': arguments,
            'id': self._msg_id
        })
        return self._msg_id

    class ServerDisconnection(Exception):
        '''Raised when the server sends a `control_disconnection` message'''

    class MultipleConnectionError(ServerDisconnection):
        '''Raised when the server detects multiple connections to the same live stream'''

    class PaidProgramDisconnection(ServerDisconnection):
        '''Raised when the streamer switches the broadcast to a paid program'''

class FC2LiveStream():
    def __init__(self, session, channel_id):
        self._meta = None
        self._session = session
        self._logger = Logger('live')
        self.channel_id = channel_id

    async def wait_for_online(self, interval):
        while not await self.is_online():
            for _ in range(interval):
                self._logger.infospin('Waiting for stream')
                await asyncio.sleep(1)

    async def is_online(self):
        meta = await self.get_meta(True)
        return len(meta['channel_data']['version']) > 0

    async def get_websocket_url(self):
        if not self.is_online:
            raise self.NotOnlineException()
        meta = await self.get_meta()
        url = 'https://live.fc2.com/api/getControlServer.php'
        data = {
            'channel_id': self.channel_id,
            'mode': 'play',
            'orz': '',
            'channel_version': meta['channel_data']['version'],
            'client_version': '2.1.0\n+[1]',
            'client_type': 'pc',
            'client_app': 'browser_hls',
            'ipv6': '',
        }
        async with self._session.post(url, data=data) as resp:
            info = await resp.json()
            return '%(url)s?control_token=%(control_token)s' % info

    async def get_meta(self, force_refetch=False):
        if self._meta is not None and not force_refetch:
            return self._meta

        url = 'https://live.fc2.com/api/memberApi.php'
        data = {
            'channel': 1,
            'profile': 1,
            'user': 1,
            'streamid': self.channel_id,
        }
        async with self._session.post(url, data=data) as resp:
            # FC2 returns text/javascript instead of application/json
            # Content type is specified so aiohttp knows what to expect
            data = await resp.json(content_type='text/javascript')
            self._meta = data['data']
            return data['data']

    class NotOnlineException(Exception):
        '''Raised when the channel is not currently broadcasting'''

class LiveStreamRecorder():
    FFMPEG_BIN = 'ffmpeg'

    def __init__(self, src, dest):
        self._logger = Logger('recording')
        self.src = src
        self.dest = dest

    async def __aenter__(self):
        self._loop = asyncio.get_running_loop()
        self._ffmpeg = await asyncio.create_subprocess_exec(
            self.FFMPEG_BIN,
            '-hide_banner', '-loglevel', 'fatal', '-stats',
            '-i', self.src, '-c', 'copy', self.dest,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        return self

    async def __aexit__(self, *err):
        ret = self._ffmpeg.returncode
        if ret is None:
            self._ffmpeg.send_signal(signal.SIGINT)
        ret = await self._ffmpeg.wait()
        self._logger.debug('exited with code', ret)

    async def print_status(self):
        try:
            status = await self.get_status()
            self._logger.infol(status['time'], status['size'])
            return True
        except:
            self._logger.endl()
            return False

    async def get_status(self):
        stderr = (await self._ffmpeg.stderr.readuntil(b'\r')).decode('utf-8')
        stats = {
            'frame': 0,
            'fps': 0,
            'q': 0,
            'size': '0kB',
            'time': '00:00:00.00',
            'bitrate': 'N/A',
            'speed': 'N/A',
        }
        last_item = '-'
        parts = [x for x in stderr.split(' ') if len(x) > 0]
        for item in parts:
            if last_item[-1] == '=':
                stats[last_item[:-1]] = item
            elif '=' in item:
                k, v = item.split('=')
                stats[k] = v
            last_item = item
        return stats

class FC2LiveDL():
    # Constants
    STREAM_QUALITY = {
        '150Kbps': 10,
        '400Kbps': 20,
        '1.2Mbps': 30,
        '2Mbps': 40,
        '3Mbps': 50,
        'sound': 90,
    }
    STREAM_LATENCY = {
        'low': 0,
        'high': 1,
        'mid': 2,
    }

    # Default params
    params = {
        'quality': '3Mbps',
        'latency': 'mid',
        'outtmpl': '%(channel_id)s-%(date)s-%(title)s.%(ext)s',
        'save_chat': False,
        'wait_for_live': False,
        'wait_poll_interval': 5,
    }

    _session = None
    _background_tasks = []

    def __init__(self, params={}):
        self._logger = Logger('fc2')
        self.params.update(params)
        # Validate outtmpl
        self._format_outtmpl()

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *err):
        await self._session.close()
        self._session = None

    async def download(self, channel_id):
        try:
            live = FC2LiveStream(self._session, channel_id)

            is_online = await live.is_online()
            if not is_online:
                if not self.params['wait_for_live']:
                    raise FC2LiveStream.NotOnlineException()
                await live.wait_for_online(self.params['wait_poll_interval'])

            self._logger.info('Stream is online')

            meta = await live.get_meta()
            ws_url = await live.get_websocket_url()
            self._logger.info('Found websocket url')
            async with FC2WebSocket(self._session, ws_url) as ws:
                hls_info = await ws.get_hls_information()
                hls_url = self._get_hls_url(hls_info)
                self._logger.info('Received HLS info')

                fname = self._format_outtmpl(meta, { 'ext': 'ts' })
                self._logger.info('Saving to ' + fname)

                async with LiveStreamRecorder(hls_url, fname) as ffmpeg:
                    self._logger.infol('Starting download')
                    while await ffmpeg.print_status():
                        pass
        except asyncio.CancelledError:
            self._logger.info('Interrupted by user')

    def _get_hls_url(self, hls_info):
        mode = self._get_mode()
        p_merged = self._merge_playlists(hls_info)
        p_sorted = self._sort_playlists(p_merged)
        playlist = self._get_playlist_or_best(p_sorted, mode)
        return playlist['url']

    def _get_playlist_or_best(self, sorted_playlists, mode=None):
        playlist = None
        for p in sorted_playlists:
            if p['mode'] == mode:
                playlist = p

        if playlist is None:
            playlist = sorted_playlists[0]

        return playlist

    def _sort_playlists(self, merged_playlists):
        def key_map(playlist):
            mode = playlist['mode']
            if mode >= 90:
                return mode - 90
            return mode

        return sorted(
            merged_playlists,
            reverse=True,
            key=key_map
        )

    def _merge_playlists(self, hls_info):
        playlists = []
        for name in ['playlists', 'playlists_high_latency', 'playlists_middle_latency']:
            if name in hls_info:
                playlists.extend(hls_info[name])
        return playlists

    def _get_mode(self):
        mode = 0
        mode += self.STREAM_QUALITY[self.params['quality']]
        mode += self.STREAM_LATENCY[self.params['latency']]
        return mode

    def _format_mode(self, mode):
        def dict_search(haystack, needle):
            return list(haystack.keys())[list(haystack.values()).index(needle)]
        latency = dict_search(self.STREAM_LATENCY, mode % 10)
        quality = dict_search(self.STREAM_QUALITY, mode // 10 * 10)
        return quality, latency

    def _format_outtmpl(self, meta=None, overrides={}):
        finfo = {
            'channel_id': '',
            'channel_name': '',
            'date': datetime.now().strftime('%F_%H%M%S'),
            'title': '',
            'ext': ''
        }

        if meta is not None:
            finfo['channel_id'] = sanitize_filename(meta['channel_data']['channelid'])
            finfo['channel_name'] = sanitize_filename(meta['profile_data']['name'])
            finfo['title'] = sanitize_filename(meta['channel_data']['title'])

        finfo.update(overrides)

        formatted = self.params['outtmpl'] % finfo
        if formatted.startswith('-'):
            formatted = '_' + formatted

        return formatted

class SmartFormatter(argparse.HelpFormatter):
    def flatten(self, input_array):
        result_array = []
        for element in input_array:
            if isinstance(element, str):
                result_array.append(element)
            elif isinstance(element, list):
                result_array += self.flatten(element)
        return result_array

    def _split_lines(self, text, width):
        if text.startswith('R|'):
            return text[2:].splitlines()  
        elif text.startswith('A|'):
            return self.flatten(
                [
                    argparse.HelpFormatter._split_lines(self, x, width)
                        if len(x) >= width else x
                    for x in text[2:].splitlines()
                ]
            )
        return argparse.HelpFormatter._split_lines(self, text, width)

async def main(args):
    parser = argparse.ArgumentParser(formatter_class=SmartFormatter)
    parser.add_argument('url',
        help='A live.fc2.com URL.'
    )
    parser.add_argument(
        '--quality',
        choices=FC2LiveDL.STREAM_QUALITY.keys(),
        default=FC2LiveDL.params['quality'],
        help='Quality of the stream to download. Default is {}.'.format(FC2LiveDL.params['quality'])
    )
    parser.add_argument(
        '--latency',
        choices=FC2LiveDL.STREAM_LATENCY.keys(),
        default=FC2LiveDL.params['latency'],
        help='Stream latency. Select a higher latency if experiencing stability issues. Default is {}.'.format(FC2LiveDL.params['latency'])
    )
    parser.add_argument(
        '-o', '--output',
        default=FC2LiveDL.params['outtmpl'],
        help='''A|Set the output filename format. Supports formatting options similar to youtube-dl. Default is '{}'

Available format options:
    channel_id (string): ID of the broadcast
    channel_name (string): broadcaster's profile name
    date (string): current date and time in the format YYYY-MM-DD_HHMMSS
    ext (string): file extension
    title (string): title of the live broadcast'''.format(FC2LiveDL.params['outtmpl'].replace('%', '%%'))
    )

    parser.add_argument(
        '--save-chat',
        action='store_true',
        help='Save live chat into a json file.'
    )
    parser.add_argument(
        '--wait',
        action='store_true',
        help='Wait until the broadcast goes live, then start recording.'
    )
    parser.add_argument(
        '--poll-interval',
        type=float,
        default=FC2LiveDL.params['wait_poll_interval'],
        help='How many seconds between checks to see if broadcast is live. Default is {}.'.format(FC2LiveDL.params['wait_poll_interval'])
    )

    # Init fc2-live-dl
    args = parser.parse_args(args[1:])
    params = {
        'quality': args.quality,
        'latency': args.latency,
        'outtmpl': args.output,
        'save_chat': args.save_chat,
        'wait_for_live': args.wait,
        'wait_poll_interval': args.poll_interval,
    }
    channel_id = args.url.split('https://live.fc2.com')[1].split('/')[1]
    async with FC2LiveDL(params) as fc2:
        try:
            await fc2.download(channel_id)
        except FC2LiveStream.NotOnlineException:
            print('Stream is not online')

if __name__ == '__main__':
    # Set up asyncio loop
    loop = asyncio.get_event_loop()
    task = asyncio.ensure_future(main(sys.argv))
    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        loop.run_until_complete(task)
    finally:
        # Give some time for aiohttp cleanup
        loop.run_until_complete(asyncio.sleep(0.250))
        loop.close()
