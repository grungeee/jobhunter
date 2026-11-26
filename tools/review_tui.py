"""Fullscreen cover-letter revision, with explicit editing and approval states."""

import curses
import difflib
from queue import Empty, SimpleQueue
import textwrap
from threading import Thread
import uuid

import generate
import review


class Revision:
    def __init__(self, block, language, index, *, save_letter=None):
        self.original = {**block, 'text': block['text'].removeprefix('# ')}
        self.language, self.index = language, index
        self.session = 'Jobhunter revision ' + uuid.uuid4().hex[:16]
        self.editor = ''
        self.cursor = 0
        self.feedback = []
        self.latest = self.proposal = None
        self.save_letter = save_letter
        self.saved_letter = None
        self.mode = 'draft'
        self.pending = ''
        self.notice = ''
        self.scroll = self.max_scroll = 0
        self.results = SimpleQueue()

    def start(self, kind, fn, *args):
        self.pending = kind
        self.notice = ''

        def worker():
            try:
                self.results.put((kind, fn(*args), ''))
            except Exception as exc:
                self.results.put((kind, None, review.terminal_text(exc) or 'Unexpected error.'))

        Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            kind, value, error = self.results.get_nowait()
        except Empty:
            return
        self.pending = ''
        if error:
            self.notice = error
            return
        self.scroll = 0
        if kind == 'flash':
            self.feedback.append(self.editor.strip())
            self.latest, self.editor, self.cursor = value, '', 0
        else:
            if value[0] == value[1]:
                self.notice = 'No reusable profile changes found.'
            else:
                self.mode, self.proposal = 'proposal', value

    def key(self, key):
        if key == curses.KEY_RESIZE or self.pending:
            return False
        if key == '\x1b':
            if self.mode == 'saved':
                return True
            if self.mode == 'confirm_letter':
                self.mode = 'draft'
            elif self.mode == 'confirm':
                self.mode = 'proposal'
            elif self.mode == 'proposal':
                self.mode, self.scroll = 'draft', 0
            else:
                return True
            self.notice = ''
            return False
        if self.mode == 'saved':
            return key in ('\n', '\r')
        if self.mode == 'confirm_letter':
            if key in ('\n', '\r') and self.save_letter is not None and self.latest is not None:
                try:
                    self.saved_letter = self.save_letter(self.latest)
                    self.notice = f'Saved as v{self.saved_letter}. No profile changes.'
                except (OSError, ValueError, review.Error) as exc:
                    self.notice = review.terminal_text(exc)
                self.mode = 'draft'
            return False
        if key in (curses.KEY_PPAGE, curses.KEY_NPAGE, curses.KEY_UP, curses.KEY_DOWN):
            if self.mode != 'confirm':
                amount = {curses.KEY_PPAGE: -5, curses.KEY_NPAGE: 5, curses.KEY_UP: -1, curses.KEY_DOWN: 1}[key]
                self.scroll = min(self.max_scroll, max(0, self.scroll + amount))
            return False
        if self.mode == 'confirm':
            if key in ('\n', '\r') and self.proposal is not None:
                try:
                    review.save_profile(*self.proposal)
                    if generate.generation_profile() != self.proposal[1]:
                        raise review.Error('The saved profile did not match this proposal.')
                except (OSError, review.Error) as exc:
                    self.mode, self.notice = 'proposal', str(exc)
                else:
                    self.mode, self.notice = 'saved', ''
            return False
        if self.mode == 'proposal':
            if key == 'a':
                if self.scroll < self.max_scroll:
                    self.notice = 'Scroll to the end of the proposed changes first.'
                else:
                    self.mode, self.notice = 'confirm', ''
            return False
        if key == '\t' and self.latest and not self.editor.strip():
            self.start('pro', review.profile_proposal, self.session, self.original, self.latest, list(self.feedback))
        elif self.saved_letter is not None:
            return key in ('\n', '\r')
        elif key == curses.KEY_F2 and self.save_letter is not None and self.latest and not self.editor.strip():
            self.mode, self.notice = 'confirm_letter', ''
        elif key in ('\n', '\r') and self.editor.strip():
            if len(self.feedback) < 20:
                self.start('flash', review.revise_block, self.session, self.original, self.language,
                           self.latest, self.editor.strip())
            else:
                self.notice = '20 revision turns reached. Clear the composer to review profile changes.'
        elif key in (curses.KEY_BACKSPACE, '\x7f', '\b') and self.cursor:
            self.editor = self.editor[:self.cursor - 1] + self.editor[self.cursor:]
            self.cursor -= 1
        elif key == curses.KEY_DC:
            self.editor = self.editor[:self.cursor] + self.editor[self.cursor + 1:]
        elif key in (curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_HOME, curses.KEY_END):
            self.cursor = {curses.KEY_HOME: 0, curses.KEY_END: len(self.editor),
                           curses.KEY_LEFT: max(0, self.cursor - 1), curses.KEY_RIGHT: min(len(self.editor), self.cursor + 1)}[key]
        elif key == '\x15':
            self.editor, self.cursor = '', 0
        elif isinstance(key, str) and key.isprintable() and len(self.editor) < 4000:
            self.editor = self.editor[:self.cursor] + key + self.editor[self.cursor:]
            self.cursor += 1
        return False


def wrap(text, width):
    return textwrap.wrap(review.terminal_text(text), max(1, width)) or ['']


def card(label, body, width, height):
    name = ' ' + label[:width - 4] + ' '
    return [('╭' + name + '─' * (width - len(name) - 2) + '╮', 'accent')] + [
        ('│ ' + value.ljust(width - 4) + ' │', 'body') for value in (body + [''] * height)[:height]] + [
        ('╰' + '─' * (width - 2) + '╯', 'muted')]


def profile_lines(base, proposed, width):
    rows = []
    for field, name in (('application_preferences', 'WRITING RULES'), ('cv_facts', 'CV FACTS FOR GENERATION')):
        if base[field] == proposed[field]:
            continue
        rows.append((name, 'accent'))
        old, new = base[field], proposed[field]
        matcher = difflib.SequenceMatcher(None, [review.canonical(x) for x in old],
                                          [review.canonical(x) for x in new], autojunk=False)
        for tag, a, b, c, d in matcher.get_opcodes():
            if tag == 'equal':
                continue
            for side, marker, style in ((old[a:b], '- ', 'removed'), (new[c:d], '+ ', 'added')):
                for item in side:
                    text = str(item) if isinstance(item, str) else f"{item['fact']} (source: {item['source']})"
                    rows.extend(((marker if i == 0 else '  ') + line, style)
                                for i, line in enumerate(wrap(text, width - 2)))
        rows.append(('', 'body'))
    return rows


def frame(state, width, height):
    if width < 42 or height < 15:
        state.max_scroll = 1  # too small to inspect or confirm a proposal
        return [('Resize to at least 42 × 15, or Esc to go back.'[:width - 1], 'muted')]
    width -= 2
    busy = {'flash': 'Flash is revising this block…', 'pro': 'Pro is checking for reusable profile changes…'}.get(state.pending)
    footer = (' ' + (busy or state.notice), 'accent' if busy else 'muted') if (busy or state.notice) else ('', 'body')
    if state.mode == 'saved':
        return [(' Jobhunter  /  Profile updated', 'accent'), ('', 'body'),
                (' Approved changes will be used on the next generation.', 'added'),
                (' Profile approval does not edit your letter or CV.', 'muted'),
                ('', 'body'), (' Enter / Esc   Return to the letter', 'accent')]
    stage = 'Review changes' if state.mode in ('proposal', 'confirm') else 'Revise block'
    header = [(f' Jobhunter  /  {stage}  {state.index:02}', 'accent'),
              (' Prepared by Pro  ·  Nothing saved yet' if state.mode in ('proposal', 'confirm') else
               ' Flash drafts wording  ·  Pro reviews lasting rules', 'muted'), ('', 'body')]
    if state.mode in ('proposal', 'confirm'):
        base, proposed = state.proposal
        rows = profile_lines(base, proposed, width - 1)
        count = height - 7
        state.max_scroll = max(0, len(rows) - count)
        state.scroll = min(state.scroll, state.max_scroll)
        if state.mode == 'confirm' and state.scroll < state.max_scroll:
            state.mode = 'proposal'
        if state.mode == 'confirm':
            hint = 'Enter save exactly this proposal   Esc back'
            footer = (' Save these changes to future generation only?', 'added')
        else:
            hint = ('↓ / PgDn inspect   Esc back' if state.scroll < state.max_scroll else 'a Approve changes   Esc back')
        return header + rows[state.scroll:state.scroll + count] + [('', 'body'), footer, (hint, 'accent')]
    left_width = (width - 2) // 2
    right_width = width - 2 - left_width
    original = wrap(state.original['text'], left_width - 4)
    if state.original.get('why'):
        original += [''] + wrap('Why: ' + state.original['why'], left_width - 4)
    revised = (wrap(state.latest['text'], right_width - 4) + [''] + wrap('Why: ' + state.latest['reason'], right_width - 4)
               if state.latest else wrap('Your revised wording will appear here.', right_width - 4))
    body_h = min(height - 13, max(4, len(original), len(revised)))
    state.max_scroll = max(0, max(len(original), len(revised)) - body_h)
    state.scroll = min(state.scroll, state.max_scroll)
    left = card('Original block', original[state.scroll:], left_width, body_h)
    right = card('Suggested revision' if right_width >= 22 else 'Suggestion', revised[state.scroll:], right_width, body_h)
    box = [(a + '  ' + b, style) for (a, style), (b, _) in zip(left, right)]
    label = 'Your feedback'
    if state.editor:
        start = max(0, state.cursor - (width - 8))
        visible = state.editor[start:start + width - 5]
        at = state.cursor - start
        entered = visible[:at] + '▏' + visible[at:]
    else:
        entered = 'Type what you want changed.'
    compose = card(label, [entered], width, 1)
    if state.mode == 'confirm_letter':
        footer = (' Save this wording to this letter only?', 'added')
        hints = [('Enter save new version   Esc cancel', 'accent'), ('No profile or CV changes', 'muted')]
    elif state.saved_letter is not None:
        hints = [('Tab Review profile changes', 'accent'), ('Enter / Esc Return to the letter', 'accent')]
    elif state.latest and not state.editor.strip():
        hints = [('F2 Save to this letter   Esc back' if state.save_letter is not None else 'Enter send feedback  ·  Esc back',
                  'accent'), ('Tab Review profile changes', 'accent')]
    else:
        hints = [('Enter send feedback  ·  Esc back', 'accent')]
    return header + box + [('', 'body')] + compose + [footer] + hints


def screen_loop(screen, state, render=frame):
    screen.keypad(True)
    screen.timeout(100)
    curses.set_escdelay(30)
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    styles = {'body': 0, 'muted': curses.A_DIM, 'accent': curses.A_BOLD,
              'added': curses.A_BOLD, 'removed': curses.A_DIM}
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for pair, (role, color) in enumerate((('accent', curses.COLOR_CYAN), ('added', curses.COLOR_GREEN),
                                               ('removed', curses.COLOR_RED)), 1):
            curses.init_pair(pair, color, -1)
            styles[role] |= curses.color_pair(pair)
    previous = None
    while True:
        state.poll()
        height, width = screen.getmaxyx()
        rows = render(state, width, height)
        fingerprint = (height, width, rows)
        if fingerprint != previous:
            screen.erase()
            for i, (line, role) in enumerate(rows[:height - 1]):
                try:
                    screen.addnstr(i, 0, line, width - 1, styles[role])
                except curses.error:
                    pass
            screen.refresh()
            previous = fingerprint
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if (width < 42 or height < 15) and key != '\x1b':
            continue
        was = state.mode
        if state.key(key):
            return 0
        if state.mode in ('confirm', 'confirm_letter') and was != state.mode:
            curses.flushinp()  # do not let queued input approve a newly drawn confirmation


def run(block, language, index, *, save_letter=None):
    try:
        return curses.wrapper(screen_loop, Revision(block, language, index, save_letter=save_letter))
    except curses.error as exc:
        raise review.Error('Could not open the revision workspace. Try jobhunter revise with --plain.') from exc
