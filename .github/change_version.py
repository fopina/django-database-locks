#!/usr/bin/env python3

import argparse
import re
from pathlib import Path

VERSION_PY = Path(__file__).parent.parent / 'database_locks' / '__init__.py'
VERSION_RE = re.compile(r"__version__ = version = '(.*?)'")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--set', metavar='VERSION', help='Set the package version')
    args = parser.parse_args()

    data = VERSION_PY.read_text()
    match = VERSION_RE.search(data)
    if match is None:
        parser.error(f'could not find a package version in {VERSION_PY}')

    current_version = match.group(1)
    print(f'Current version: {current_version}')

    if args.set:
        version = args.set[1:] if args.set.startswith('v') else args.set
        updated = VERSION_RE.sub(
            lambda _: f"__version__ = version = '{version}'", data, count=1
        )
        VERSION_PY.write_text(updated)
        print(f'New version: {version}')


if __name__ == '__main__':
    main()
