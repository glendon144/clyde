"""Create a client-only archive; refuse to overwrite existing files."""
import argparse
from pathlib import Path
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    with tarfile.open(args.output, 'x:gz') as archive:
        for name in ('__init__.py', 'protocol.py', 'proposals.py', 'controller.py'):
            archive.add(Path(__file__).parent / name, arcname='clyde/' + name)


if __name__ == '__main__':
    main()
