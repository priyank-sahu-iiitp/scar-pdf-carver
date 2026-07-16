import re
import sys


class Lexer:
    # Token-based state machine lexer
    tokens = []
    error = 0
    verbose = 0
    state_stack = []
    processed = 0
    processed_buffer = b''
    saved_state = None

    def __init__(self, verbose=0):
        if not self.verbose:
            self.verbose = verbose
        self.buffer = bytearray()
        for row in self.tokens:
            row.append(re.compile(row[0], re.DOTALL))
            content_pat = row[1]
            if isinstance(content_pat, str):
                content_pat = content_pat.encode('latin-1')
            row.append(re.compile(content_pat, re.DOTALL))

    def save_state(self, t=None, m=None):
        if self.error:
            return
        if self.verbose > 2:
            print("Saving state %s\n" % self.processed)
        try:
            end = m.end()
        except AttributeError:
            end = 0
        self.saved_state = dict(
            state_stack=self.state_stack[:],
            processed=self.processed - end,
            processed_buffer=self.processed_buffer,
            readptr=self.fd.tell() - len(self.buffer) - end,
            state=self.state,
            objects=self.objects[:],
            error=self.error,
        )

    def restore_state(self):
        if self.verbose > 2:
            print("Restoring state")
        state = self.saved_state
        if not state:
            return
        self.state_stack = state['state_stack']
        self.processed = state['processed']
        self.processed_buffer = state['processed_buffer']
        self.buffer = bytearray()
        self.fd.seek(state['readptr'])
        self.state = state['state']
        self.objects = state['objects']
        self.error = state['error']

    def next_token(self, end=True):
        current_state = self.state
        for state_re, re_str, token, next_state_val, state, regex in self.tokens:
            if state.match(current_state):
                if self.verbose > 2:
                    print("Trying to match %r with %r" % (self.buffer[:10], re_str))
                m = regex.match(self.buffer)
                if m:
                    if self.verbose > 3:
                        print("%s matched %s" % (re_str, m.group(0)))
                    self.processed_buffer += self.buffer[:m.end()]
                    self.buffer = self.buffer[m.end():]
                    self.processed += m.end()
                    for t in token.split(','):
                        try:
                            if self.verbose > 0:
                                print("0x%X: Calling %s %r" % (self.processed, t, m.group(0)))
                            cb = getattr(self, t, self.default_handler)
                        except AttributeError:
                            continue
                        new_state = cb(t, m)
                        if new_state == "CONTINUE":
                            continue
                        elif new_state:
                            next_state_val = new_state
                            self.state = next_state_val
                    if next_state_val:
                        self.state = next_state_val
                    return token

        if (end and len(self.buffer) > 0) or len(self.buffer) > 1024:
            self.processed_buffer += self.buffer[:1]
            self.buffer = self.buffer[1:]
            self.ERROR("Lexer Stuck, discarding 1 byte (%r) - state %s"
                       % (self.buffer[:10], self.state))
            return "ERROR"
        return None

    def feed(self, data):
        self.buffer.extend(data)

    def empty(self):
        return not len(self.buffer)

    def default_handler(self, token, match):
        if self.verbose > 2:
            print("Default handler: %s with %r" % (token, match.group(0)))

    def ERROR(self, message=None, weight=1):
        if message:
            print("Error(%s): %s" % (weight, message))
        self.error += weight

    def PUSH_STATE(self, token=None, match=None):
        if self.verbose > 1:
            print("Storing state %s" % self.state)
        self.state_stack.append(self.state)

    def POP_STATE(self, token=None, match=None):
        try:
            state = self.state_stack.pop()
            if self.verbose > 1:
                print("Returned state to %s" % state)
            return state
        except IndexError:
            print("Tried to pop the state but failed - possible recursion error")
            return None


# Self-feeding variant that reads from fd automatically
class SelfFeederMixIn(Lexer):
    def next_token(self):
        if len(self.buffer) < 512:
            self.feed()
        return Lexer.next_token(self)

    def feed(self, size=512):
        data = self.fd.read(size)
        Lexer.feed(self, data)
        return len(data)
