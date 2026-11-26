#!/usr/bin/env python3
"""Browse existing versioned CV PDFs and compare extracted text, read-only."""

import argparse
import curses
import difflib
import functools
import os
from pathlib import Path
import re
import subprocess
import textwrap
import unicodedata

SOURCE = Path(os.environ.get('JOBHUNTER_CV_SOURCE', str(Path(__file__).resolve().parents[2] / 'src')))
VERSION = re.compile(r'cv_v(\d+\.\d+\.\d+)(?:_\d{4}-\d{2}-\d{2})?\.pdf')


def versions(directory=SOURCE):
    candidates = {}
    for path in sorted(directory.glob('cv_v*.pdf')):
        match = VERSION.fullmatch(path.name)
        if match and path.is_file():
            version = match.group(1)
            if version in candidates:
                raise ValueError(f'Multiple PDF files claim CV {version}.')
            candidates[version] = path
    return dict(sorted(candidates.items(), key=lambda pair: tuple(map(int, pair[0].split('.')))))


def extract(path):
    result = subprocess.run(['pdftotext', '-layout', '-nopgbrk', str(path), '-'],
                            capture_output=True, encoding='utf-8', timeout=15, check=True)
    # Ignore line-end layout padding, but keep text and blank lines intact.
    lines = [''.join(c for c in row.expandtabs() if not unicodedata.category(c).startswith('C')).rstrip()
             for row in result.stdout.strip('\n').splitlines()]
    if not any(line.strip() for line in lines):
        raise ValueError(f'No extractable text in {path.name}.')
    return lines


def diff(old, old_lines, new, new_lines):
    return list(difflib.unified_diff(old_lines, new_lines, f'CV {old}', f'CV {new}', lineterm=''))


def browse(screen, files):
    names = list(files)
    baseline, selected = max(0, len(names) - 2), len(names) - 1
    mode, offset = 'current', 0
    read = functools.lru_cache(maxsize=8)(lambda version: extract(files[version]))
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_RED, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)

    def show(row, line, style=0):
        try:
            screen.addnstr(row, 0, line, max(0, columns - 1), style)
        except curses.error:
            pass

    while True:
        rows, columns = screen.getmaxyx()
        page = max(1, rows - 5)
        screen.erase()
        old, cur = names[baseline], names[selected]
        version = old if mode == 'baseline' else cur
        try:
            lines = ((diff(old, read(old), cur, read(cur)) or ['No text differences.'])
                     if mode == 'diff' else read(version))
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            lines = [f'Cannot read this version: {exc}']
        display = []
        for line in lines:
            colour = 0
            if mode == 'diff' and curses.has_colors():
                colour = (3 if line.startswith(('@@', '+++', '---')) else
                          1 if line.startswith('+') else 2 if line.startswith('-') else 0)
            wrapped = textwrap.wrap(line, max(columns - 2, 1), replace_whitespace=False,
                                    drop_whitespace=False) or ['']
            display.extend((part, curses.color_pair(colour)) for part in wrapped)
        offset = max(0, min(offset, len(display) - page))
        title = f'CV history | diff {old} -> {cur}' if mode == 'diff' else f'CV {version} | {mode} view'
        show(0, title, curses.A_BOLD)
        show(1, f'Baseline {old}   Selected {cur}   ({selected + 1}/{len(names)} versions)')
        for row, (line, style) in enumerate(display[offset:offset + page], 2):
            show(row, line, style)
        show(rows - 3, '[ / ] select older/newer   { / } change baseline   t toggle')
        show(rows - 2, 'd diff   j/k scroll   PgUp/PgDn   g/G top/bottom   q back')
        screen.refresh()
        key = screen.getch()
        if key in (ord('q'), 27):
            return
        if key in (ord('['), ord(']'), ord('{'), ord('}'), ord('t'), ord('d')):
            offset = 0
            if key in (ord('['), ord(']')):
                selected = min(len(names) - 1, max(0, selected + (1 if key == ord(']') else -1)))
                if mode == 'baseline':
                    mode = 'current'
            elif key in (ord('{'), ord('}')):
                baseline = min(len(names) - 1, max(0, baseline + (1 if key == ord('}') else -1)))
                if mode != 'diff':
                    mode = 'baseline'
            elif key == ord('t'):
                mode = 'current' if mode == 'baseline' else 'baseline'
            else:
                mode = 'current' if mode == 'diff' else 'diff'
        elif key in (ord('j'), curses.KEY_DOWN, ord('k'), curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
            offset += {curses.KEY_NPAGE: page, curses.KEY_PPAGE: -page}.get(key, 1 if key in (ord('j'), curses.KEY_DOWN) else -1)
        elif key in (ord('g'), ord('G')):
            offset = 0 if key == ord('g') else len(display)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--list', action='store_true')
    actions.add_argument('--diff', nargs=2, metavar=('OLD', 'NEW'))
    args = parser.parse_args(argv)
    try:
        files = versions()
        if not files:
            raise ValueError('No versioned CV PDFs found.')
        if args.list:
            print('\n'.join(files))
        elif args.diff:
            old, new = args.diff
            if old not in files or new not in files:
                raise ValueError('Unknown CV version. Run jobhunter cv --list.')
            print('\n'.join(diff(old, extract(files[old]), new, extract(files[new]))) or 'No text differences.')
        else:
            curses.wrapper(browse, files)
    except (ValueError, OSError, subprocess.SubprocessError, curses.error) as exc:
        parser.exit(1, f'CV history: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
