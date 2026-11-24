# Copyright (C) 2026 KuraLabs S.R.L.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Command line: ``sintonia serve``, ``sintonia check``, ``sintonia import``.
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .checks import CheckError, preflight
from .config import ConfigError, load
from .scan import parse, to_channels, to_toml

DEFAULT_CONFIG = os.environ.get('SINTONIA_CONFIG', '/config/config.toml')

log = logging.getLogger('sintonia')


def _setup_logging(verbose):
    """
    :param bool verbose: Log debug messages.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
    # One line per MediaMTX API poll is noise.
    logging.getLogger('httpx2').setLevel(logging.WARNING)


def _load_checked(path):
    """
    Load the configuration and run the start-up checks.

    :param str path: Configuration file.

    :return: The configuration and the adapters to use.
    :rtype: tuple

    :raises SystemExit: With a message if anything is wrong.
    """
    try:
        config = load(path)
        adapters = preflight(config)
    except (ConfigError, CheckError) as e:
        log.error('%s', e)
        raise SystemExit(1) from e
    return config, adapters


def serve(args):
    import uvicorn

    from .app import create_app

    config, adapters = _load_checked(args.config)
    log.info(
        'sintonia %s: %d channels, tuners %s, %s transcoding',
        __version__, len(config.channels),
        ', '.join(map(str, adapters)), config.transcode.hwaccel,
    )
    uvicorn.run(
        create_app(config, adapters),
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        access_log=args.verbose,
    )


def check(args):
    config, adapters = _load_checked(args.config)
    print(
        f'ok: {len(config.channels)} channels, tuners '
        f'{", ".join(map(str, adapters))}, '
        f'{config.transcode.hwaccel} transcoding works'
    )


def import_(args):
    text = Path(args.file).read_text()
    channels = to_channels(parse(text), include_oneseg=args.oneseg)
    if not channels:
        log.error('no services found in %s', args.file)
        raise SystemExit(1)
    sys.stdout.write(to_toml(channels))


def main(argv=None):
    """
    Entry point.

    :param list argv: Arguments, without the program name.
    """
    parser = argparse.ArgumentParser(
        prog='sintonia',
        description='Simple web app for live over-the-air TV.',
    )
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {__version__}',
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)

    for name, function, text in (
        ('serve', serve, 'run the server'),
        ('check', check, 'validate the configuration and the hardware'),
    ):
        sub = commands.add_parser(name, help=text)
        sub.add_argument(
            '-c', '--config', default=DEFAULT_CONFIG,
            help=f'configuration file (default: {DEFAULT_CONFIG})',
        )
        sub.set_defaults(function=function)

    sub = commands.add_parser(
        'import', help='convert a dvbv5-scan channel file to [[channels]]',
    )
    sub.add_argument('file', help='dvbv5-scan output file')
    sub.add_argument(
        '--oneseg', action='store_true',
        help='include one-seg (mobile, low resolution) services',
    )
    sub.set_defaults(function=import_)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    args.function(args)


if __name__ == '__main__':
    main()
