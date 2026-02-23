#!/usr/bin/env python3
"""
Email automator

Call custom workflows to automatically check/send emails using the
appropriate logic for various platforms.
"""

import argparse
import logging
import sys
from dotenv import load_dotenv
from crossref_error_report import CrossrefEmailProcessor
from muse_locations_report import MUSEEmailProcessor

AUTOMATORS = {
    "Crossref": CrossrefEmailProcessor,
    "ProjectMUSE": MUSEEmailProcessor,
}

AUTOMATORS_STR = ', '.join("%s" % (key) for (key, _) in AUTOMATORS.items())

ARGS = [
    {
        "val": "--automation",
        "dest": "automation",
        "action": "store",
        "help": "Email automation to use. One of: {}".format(AUTOMATORS_STR)
    }
]


def run(automation):
    """Execute an email automation based on input parameters"""
    logging.info(f'Beginning {automation} automated email workflow')
    try:
        automator = AUTOMATORS[automation]
    except KeyError:
        logging.error(
            f'{automation} automation not supported: '
            f'platform must be one of {AUTOMATORS_STR}'
        )
        sys.exit(1)
    automator.run()


def get_arguments():
    """Parse input arguments using ARGS"""
    parser = argparse.ArgumentParser()
    for arg in ARGS:
        if 'default' in arg:
            parser.add_argument(arg["val"], dest=arg["dest"],
                                default=arg["default"], action=arg["action"],
                                help=arg["help"])
        else:
            parser.add_argument(arg["val"], dest=arg["dest"], required=True,
                                action=arg["action"], help=arg["help"])
    args = parser.parse_args()
    return args


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO,
                        format='%(levelname)s:%(asctime)s: %(message)s')
    # Load config for local development (GitHub Actions uses Secrets)
    load_dotenv('./config.env')
    ARGUMENTS = get_arguments()
    run(ARGUMENTS.automation)
