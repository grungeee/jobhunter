#!/usr/bin/env python3
"""Browse, compare and restore the cover-letter versions for one listing."""

import argparse
import curses
import difflib
import functools

import generate
from review_tui import screen_loop, wrap


def compare(old, old_text, new, new_text):
    return list(difflib.unified_diff(old_text.splitlines(), new_text.splitlines(),
                                     f'v{old}', f'v{new}', lineterm=''))


class History:
    def __init__(self, out_root, listing_id, versions):
        self.out_root, self.listing_id, self.versions = out_root, listing_id, versions
        self.numbers = list(versions)
        self.active = next((number for number, meta in versions.items() if meta['current']), None)
        self.selected = max(0, len(self.numbers) - 2) if self.active is not None else len(self.numbers) - 1
        self.mode, self.notice = 'letter', ''
        self.scroll = self.max_scroll = 0
        self.restored = None
        self.text = functools.lru_cache(maxsize=8)(
            lambda number: generate.read_letter_version(out_root, listing_id, number)['cover-letter.md'])

    def poll(self):
        pass

    def key(self, key):
        if self.mode == 'confirm':
            self.mode = 'letter'
            if key in ('\n', '\r'):
                try:
                    self.restored = generate.restore_letter_version(self.out_root, self.listing_id,
                                                                   self.numbers[self.selected], expected=self.active)
                    self.mode = 'restored'
                except (generate.GenerateError, OSError) as exc:
                    self.notice = str(exc)
            return False
        if self.mode == 'restored':
            return key in ('\n', '\r', 'q', '\x1b')
        if key in ('q', '\x1b'):
            return True
        if key in ('[', ']'):
            self.selected = min(len(self.numbers) - 1, max(0, self.selected + (1 if key == ']' else -1)))
            self.scroll = 0
        elif key == 'd':
            self.mode = 'letter' if self.mode == 'diff' else 'diff'
            self.scroll = 0
        elif key == 'r' and self.numbers[self.selected] != self.active:
            self.mode = 'confirm'
        elif key in ('j', 'k', curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
            step = {'j': 1, 'k': -1, curses.KEY_DOWN: 1, curses.KEY_UP: -1,
                    curses.KEY_NPAGE: 10, curses.KEY_PPAGE: -10}[key]
            self.scroll = min(self.max_scroll, max(0, self.scroll + step))
        return False


def frame(state, width, height):
    if width < 42 or height < 15:
        return [('Resize to 42 x 15, or Esc to go back.'[:max(width - 1, 0)], 'muted')]
    header = [(' Jobhunter  /  Cover-letter versions', 'accent')]
    if state.mode == 'restored':
        return header + [('', 'body'), (f' Restored as v{state.restored}. Earlier versions were kept.', 'added'),
                         (' Your CV was not changed.', 'muted'), (' Enter / Esc   Return to the letter', 'accent')]
    selected = state.numbers[state.selected]
    baseline = state.active or state.numbers[-1]
    meta = state.versions[selected]
    stamp = meta.get('version_at') or meta.get('generated_at') or 'Undated'
    status = f'Current v{state.active}' if state.active else 'No active letter'
    header += [(f' v{selected}  ({state.selected + 1}/{len(state.numbers)})   {status}', 'body'),
               (f' {stamp}', 'muted'), ('', 'body')]
    if state.mode == 'confirm':
        return header + [(f' Restore v{selected} as a new current version?', 'added'),
                         (' This keeps all versions and does not edit your CV.', 'muted'),
                         (' Enter restore   Esc cancel', 'accent')]
    rows = []
    try:
        if state.mode == 'diff':
            lines = compare(selected, state.text(selected), baseline, state.text(baseline)) or ['No text differences.']
            header[2] = (f' Comparing v{selected} -> v{baseline}', 'muted')
        else:
            lines = state.text(selected).splitlines()
        for line in lines:
            style = 'body'
            if state.mode == 'diff':
                style = ('accent' if line.startswith(('@@', '+++', '---')) else
                         'added' if line.startswith('+') else 'removed' if line.startswith('-') else 'body')
            rows.extend((' ' + text, style) for text in wrap(line, width - 3))
    except (generate.GenerateError, OSError, KeyError) as exc:
        rows = [(' ' + str(exc), 'muted')]
    page = height - 9
    state.max_scroll = max(0, len(rows) - page)
    state.scroll = min(state.max_scroll, state.scroll)
    return header + rows[state.scroll:state.scroll + page] + [('', 'body'), (state.notice, 'muted'),
            (' [ / ] versions   d diff   j/k scroll', 'accent'),
            (' r restore   q back' if selected != state.active else ' q back', 'accent')]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('listing_id')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--list', action='store_true')
    modes.add_argument('--diff', nargs=2, type=int, metavar=('OLD', 'NEW'))
    args = parser.parse_args(argv)
    try:
        versions = generate.letter_versions(generate.DEFAULT_OUT, args.listing_id)
        if not versions:
            raise generate.GenerateError('No saved cover letter for this listing.')
        state = History(generate.DEFAULT_OUT, args.listing_id, versions)
        if args.list:
            for number, meta in versions.items():
                print(f'v{number}' + (' (current)' if meta['current'] else ''))
        elif args.diff:
            old, new = args.diff
            print('\n'.join(compare(old, state.text(old), new, state.text(new))) or 'No text differences.')
        else:
            return curses.wrapper(lambda screen: screen_loop(screen, state, frame))
    except (generate.GenerateError, OSError, KeyError, curses.error) as exc:
        parser.exit(1, f'Cover-letter versions: {exc}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
