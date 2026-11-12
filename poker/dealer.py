"""
dealer.py  (Project #2 — sandboxed, privilege-separated edition)
=================================================================
Multiplayer Texas Hold'em server.

Usage:
    sudo -u dealer python3 /home/dealer/dealer.py <num_players>
    (or start it as root; the engine drops to the `dealer` user itself)

Options:
    --timeout=<sec>     seconds a player has to act        (default 30)
    --no-sandbox        skip seccomp, for debugging only
    --require-sandbox   refuse to run if seccomp can't be installed

Process layout:
    supervisor ──fork──> engine (sandboxed; see sandbox.py)
"""

import sys, os, time, pwd, grp
from collections import Counter
from itertools import combinations

sys.path.insert(0, '/opt/poker')
sys.path.insert(1, os.path.dirname(os.path.abspath(__file__)))

from cards import Deck
from banking import BankAccount
import poker_communication as pc
from poker_communication import Server, SecureLog, render_cards
import sandbox

STARTING_BALANCE = 1000
SMALL_BLIND      = 10
BIG_BLIND        = 20
ACTION_TIMEOUT   = 30.0
RECONNECT_WAIT   = 60.0      # how long to wait for a disconnected player before ending
BETWEEN_HANDS    = 3.0

HAND_NAMES = [
    'High Card', 'One Pair', 'Two Pair', 'Three of a Kind',
    'Straight', 'Flush', 'Full House', 'Four of a Kind', 'Straight Flush',
]


def log(msg):
    print('[dealer] %s' % msg, flush=True)


def log_cards(label, cards):
    print('[dealer] %s' % label, flush=True)
    for row in render_cards(cards):
        print('         ' + row, flush=True)


# ── Hand evaluation ──────────────────────────────────────────────────────────
_FACE = {'jack': 11, 'queen': 12, 'king': 13, 'ace': 14}

def _card_value(rank):
    r = rank.strip().lower()
    return _FACE[r] if r in _FACE else int(r)


def _score_five(cards):
    """
    (category, tiebreak) — higher compares better.
    Tiebreak lists the ranks grouped by how many times they appear, so a pair
    of kings correctly beats a pair of twos with an ace kicker.  (Project #1
    sorted by rank alone, which got that comparison backwards.)
    """
    vals = sorted((_card_value(c.rank) for c in cards), reverse=True)
    groups = sorted(Counter(vals).items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    shape = [n for _, n in groups]
    ordered = [v for v, n in groups for _ in range(n)]
    flush = len({c.suit for c in cards}) == 1
    uniq = sorted(set(vals))
    high = None
    if len(uniq) == 5:
        if uniq[-1] - uniq[0] == 4:
            high = uniq[-1]
        elif uniq == [2, 3, 4, 5, 14]:
            high = 5                                   # wheel: A-2-3-4-5
    if high and flush:          return (8, [high])
    if shape[0] == 4:           return (7, ordered)
    if shape[:2] == [3, 2]:     return (6, ordered)
    if flush:                   return (5, vals)
    if high:                    return (4, [high])
    if shape[0] == 3:           return (3, ordered)
    if shape[:2] == [2, 2]:     return (2, ordered)
    if shape[0] == 2:           return (1, ordered)
    return (0, vals)


def evaluate_hand(hole, community):
    return max(_score_five(list(f)) for f in combinations(hole + community, 5))


def build_pots(contrib, folded):
    """
    Split the chips into a main pot and side pots.
    contrib  {pid: chips put in this hand}     folded  set of pids
    Returns [(amount, [eligible pids]), ...].  Chips from folded players stay
    in the pots; a layer only folded players reached goes to the pot below.
    """
    levels = sorted({c for c in contrib.values() if c > 0})
    pots, prev, carry = [], 0, 0
    for lvl in levels:
        amount = sum(min(c, lvl) - min(c, prev) for c in contrib.values()) + carry
        eligible = [p for p, c in contrib.items() if c >= lvl and p not in folded]
        prev = lvl
        if eligible and pots and pots[-1][1] == eligible:
            pots[-1][0] += amount                      # same contenders: same pot
            carry = 0
        elif eligible:
            pots.append([amount, eligible])
            carry = 0
        elif pots:
            pots[-1][0] += amount
        else:
            carry = amount
    if carry and pots:
        pots[-1][0] += carry
    return [(a, e) for a, e in pots]


# ── The table ────────────────────────────────────────────────────────────────
class FoldNow(Exception):
    pass


class Table:
    def __init__(self, server, seats, dealer_log, timeout):
        self.srv = server
        self.seats = seats                       # sorted player ids
        self.bank = {p: BankAccount(STARTING_BALANCE) for p in seats}
        self.dlog = dealer_log
        self.timeout = timeout
        self.deck = Deck()
        self.button = -1                         # index into self.seats
        self.hand = None                         # current Hand or None
        self.hand_num = 0

    # ---- events that can arrive at any moment ---------------------------
    def handle(self, ev, actor=None):
        """
        Process one event.  Returns the body if it's a message from `actor`
        (the player whose turn it is); everything else is handled here.
        """
        kind, pid = ev[0], ev[1]
        if kind == 'join':
            self.srv.send(pid, 'WELCOME:Player %d — balance %d' % (
                pid, self.bank[pid].show_balance()))
            self.srv.broadcast('INFO:Player %d joined the table.' % pid, exclude=(pid,))
            log('Player %d connected (%d/%d)' % (pid, len(self.srv.connected()),
                                                 len(self.seats)))
            if self.hand and pid in self.hand.players:
                self.srv.send(pid, 'INFO:You were folded from the current hand; '
                                   'you are back in on the next one.')
        elif kind == 'leave':
            log('Player %d disconnected: %s' % (pid, ev[2]))
            self.srv.broadcast('INFO:Player %d disconnected.' % pid)
            # Only the player who left is folded; the hand carries on for everyone
            # else.  An all-in player keeps their stake in the pot and still shows down.
            if self.hand and pid not in self.hand.allin:
                self.hand.fold(pid, 'disconnected')
        elif kind == 'msg':
            body = ev[2]
            up = body.upper()
            if pid == actor and up not in ('WHO', 'STATUS', 'HELP'):
                return body
            if up in ('WHO', 'STATUS'):
                self.send_status(pid)
            elif up == 'HELP':
                self.srv.send(pid, 'INFO:FOLD  CHECK  CALL  RAISE:<n>  ALLIN  '
                                   'CHAT:<text>  MSG:<id>:<text>  WHO')
            elif up.split(':')[0] in ('FOLD', 'CHECK', 'CALL', 'RAISE', 'ALLIN', 'BET'):
                who = self.hand.current if self.hand else None
                self.srv.send(pid, 'ERROR:Not your turn%s.' % (
                    ' — waiting for Player %d' % who if who is not None else ''))
            else:
                self.srv.send(pid, 'ERROR:Unknown command. Type HELP.')
        return None

    def pump(self, seconds):
        """Keep serving chat/joins/leaves for `seconds` (replaces time.sleep)."""
        end = time.monotonic() + seconds
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            for ev in self.srv.poll(left):
                self.handle(ev)

    def send_status(self, pid):
        parts = []
        for p in self.seats:
            tag = '' if self.srv.is_connected(p) else ' (away)'
            if self.hand and p in self.hand.players:
                if p in self.hand.folded:
                    tag += ' folded'
                elif p in self.hand.allin:
                    tag += ' all-in'
            parts.append('P%d=$%d%s' % (p, self.bank[p].show_balance(), tag))
        self.srv.send(pid, 'STATUS:' + '  '.join(parts))

    # ---- lobby ----------------------------------------------------------
    def wait_for_all(self):
        log('Waiting for %d players on %s' % (len(self.seats), pc.SOCKET_PATH))
        while len(self.srv.connected()) < len(self.seats):
            for ev in self.srv.poll(5.0):
                self.handle(ev)

    # ---- main loop ------------------------------------------------------
    def run(self):
        self.wait_for_all()
        self.srv.broadcast('GAME_START:Game on! Starting balance: %d chips.'
                           % STARTING_BALANCE)
        log('All players connected — game starting')
        try:
            while True:
                alive = [p for p in self.seats if self.bank[p].show_balance() > 0]
                if len(alive) < 2:
                    break
                seated = [p for p in alive if self.srv.is_connected(p)]
                if len(seated) < 2:
                    if not self._wait_for_reconnect(alive):
                        break
                    continue
                self.hand_num += 1
                Hand(self, seated).play()
                self.hand = None
                self.pump(BETWEEN_HANDS)
        finally:
            self.game_over()

    def _wait_for_reconnect(self, alive):
        self.srv.broadcast('INFO:Not enough players connected. Waiting up to '
                           '%d s for someone to reconnect...' % RECONNECT_WAIT)
        end = time.monotonic() + RECONNECT_WAIT
        while time.monotonic() < end:
            for ev in self.srv.poll(end - time.monotonic()):
                self.handle(ev)
            if sum(self.srv.is_connected(p) for p in alive) >= 2:
                return True
        return False

    def game_over(self):
        best = max(self.seats, key=lambda p: self.bank[p].show_balance())
        alive = [p for p in self.seats if self.bank[p].show_balance() > 0]
        if len(alive) == 1:
            msg = 'Player %d wins the game!' % alive[0]
        else:
            msg = 'Game ended. Chip leader: Player %d with $%d.' % (
                best, self.bank[best].show_balance())
        self.srv.broadcast('GAME_OVER:' + msg)
        log('GAME OVER: %s  (%d hands)' % (msg, self.hand_num))


class Hand:
    def __init__(self, table, seated):
        self.t = table
        self.srv = table.srv
        self.bank = table.bank
        # rotate the button to the next seated player
        seats = table.seats
        i = table.button
        for _ in range(len(seats)):
            i = (i + 1) % len(seats)
            if seats[i] in seated:
                break
        table.button = i
        b = seated.index(seats[i])
        self.players = seated[b:] + seated[:b]   # players[0] holds the button
        self.contrib = {p: 0 for p in self.players}
        self.street = {p: 0 for p in self.players}
        self.folded, self.allin = set(), set()
        self.to_act = []
        self.current = None
        self.community = []
        self.hole = {}
        table.hand = self

    # ---- helpers --------------------------------------------------------
    def in_hand(self):
        return [p for p in self.players if p not in self.folded]

    def can_act(self):
        return [p for p in self.players if p not in self.folded and p not in self.allin]

    def pot(self):
        return sum(self.contrib.values())

    def bet(self, p, amount):
        """Move chips from p's bank to the pot.  Never takes more than they have."""
        amount = max(0, min(amount, self.bank[p].show_balance()))
        self.bank[p].sub_balance(amount)
        self.contrib[p] += amount
        self.street[p] += amount
        if self.bank[p].show_balance() == 0:
            self.allin.add(p)
        return amount

    def fold(self, p, why):
        if p not in self.players or p in self.folded:
            return
        self.folded.add(p)
        if p in self.to_act:
            self.to_act.remove(p)
        self.srv.broadcast('INFO:Player %d folds (%s).' % (p, why))
        log('Player %d FOLD (%s)' % (p, why))

    def order_from(self, start):
        """Players in seat order starting at index `start` of self.players."""
        n = len(self.players)
        return [self.players[(start + k) % n] for k in range(n)]

    # ---- the hand -------------------------------------------------------
    def play(self):
        t, srv = self.t, self.srv
        t.deck.reset_deck()
        t.deck.shuffle()
        n = len(self.players)
        srv.broadcast('NEW_HAND:#%d — %d players. Button: Player %d.' % (
            t.hand_num, n, self.players[0]))
        log('=== Hand #%d  button=P%d ===' % (t.hand_num, self.players[0]))
        for p in self.players:
            srv.send(p, 'BALANCE:%d' % self.bank[p].show_balance())

        for p in self.players:
            self.hole[p] = [t.deck.draw_card(), t.deck.draw_card()]
            srv.send(p, 'HOLE:%s|%s' % tuple(self.hole[p]))
        t.dlog.write('Hand #%d: ' % t.hand_num + '  '.join(
            'P%d=%s, %s' % (p, *self.hole[p]) for p in self.players))

        # Blinds: heads-up the button posts small; otherwise the two seats after it.
        if n == 2:
            sb, bb = self.players[0], self.players[1]
            first_pre, first_post = 0, 1
        else:
            sb, bb = self.players[1], self.players[2]
            first_pre, first_post = 3 % n, 1
        for p, blind, name in ((sb, SMALL_BLIND, 'small'), (bb, BIG_BLIND, 'big')):
            paid = self.bet(p, blind)              # capped at their balance
            allin = ' (all-in)' if p in self.allin else ''
            srv.broadcast('INFO:Player %d posts %s blind %d%s.' % (p, name, paid, allin))
            log('P%d posts %s blind $%d%s' % (p, name, paid, allin))

        if self.betting_round('pre-flop', first_pre, BIG_BLIND):
            for name, count in (('flop', 3), ('turn', 1), ('river', 1)):
                self.community += [t.deck.draw_card() for _ in range(count)]
                log_cards('%s:' % name.upper(), self.community)
                if not self.betting_round(name, first_post, 0):
                    break
        self.finish()

    def betting_round(self, name, first, opening_bet):
        """Returns False if the hand is over (one player left)."""
        srv = self.srv
        if name != 'pre-flop':
            self.street = {p: 0 for p in self.players}
        srv.broadcast('PHASE:%s' % name)
        srv.broadcast('POT:%d' % self.pot())
        if self.community:
            srv.broadcast('COMMUNITY:%s' % '|'.join(str(c) for c in self.community))
        log('%s  pot=$%d' % (name, self.pot()))

        max_bet = max(max(self.street.values()), opening_bet if name == 'pre-flop' else 0)
        min_raise = BIG_BLIND
        self.to_act = [p for p in self.order_from(first) if p in self.can_act()]
        # Nobody to bet against: skip (everyone else is all-in and matched)
        if len(self.can_act()) <= 1 and all(
                self.street[p] >= max_bet for p in self.can_act()):
            self.to_act = []

        while self.to_act and len(self.in_hand()) > 1:
            p = self.to_act.pop(0)
            action = self.ask(p, max_bet, min_raise)
            if action is None:
                continue                            # folded (timeout / disconnect)
            kind, amount = action
            owed = max_bet - self.street[p]
            if kind == 'fold':
                self.fold(p, 'folded')
            elif kind == 'call':
                paid = self.bet(p, owed)
                word = 'checks' if paid == 0 else 'calls %d' % paid
                if p in self.allin:
                    word += ' (all-in)'
                srv.broadcast('INFO:Player %d %s.' % (p, word))
                log('P%d %s' % (p, word))
            elif kind == 'raise':                   # amount = chips to put in now
                paid = self.bet(p, amount)
                new_max = self.street[p]
                raised_by = new_max - max_bet
                if raised_by > 0:
                    if raised_by >= min_raise:
                        min_raise = raised_by
                    max_bet = new_max
                    # everyone else still able to act must respond
                    idx = self.players.index(p)
                    self.to_act = [q for q in self.order_from(idx + 1)
                                   if q != p and q in self.can_act()]
                tag = ' (all-in)' if p in self.allin else ''
                srv.broadcast('INFO:Player %d raises to %d%s.' % (p, new_max, tag))
                log('P%d raises to $%d%s' % (p, new_max, tag))
            srv.broadcast('POT:%d' % self.pot())
        self.current = None
        return len(self.in_hand()) > 1

    def ask(self, p, max_bet, min_raise):
        """
        Wait for player p's decision while still serving everyone else.
        Returns ('fold'|'call'|'raise', amount) or None if p was folded
        (timed out or disconnected).
        """
        srv, t = self.srv, self.t
        if not srv.is_connected(p):
            self.fold(p, 'away')
            return None
        self.current = p
        owed = max_bet - self.street[p]
        bal = self.bank[p].show_balance()
        srv.send(p, 'YOUR_TURN:to_call=%d balance=%d min_raise=%d pot=%d timeout=%d' % (
            min(owed, bal), bal, min_raise, self.pot(), t.timeout))
        srv.broadcast('TURN:Waiting for Player %d...' % p, exclude=(p,))
        deadline = time.monotonic() + t.timeout
        warned = False

        while True:
            if p in self.folded:                    # disconnected while we waited
                return None
            if len(self.in_hand()) <= 1:
                return None
            left = deadline - time.monotonic()
            if left <= 0:
                if owed <= 0:
                    srv.send(p, 'INFO:Time is up — you check.')
                    return ('call', 0)
                self.fold(p, 'timed out')
                return None
            if not warned and left <= 10 and t.timeout > 15:
                srv.send(p, 'INFO:10 seconds left to act.')
                warned = True
            for ev in srv.poll(min(left, 10.0 if not warned else left)):
                body = t.handle(ev, actor=p)
                if body is None:
                    continue
                result = self.parse_action(p, body, owed, bal, min_raise)
                if result:
                    return result

    def parse_action(self, p, body, owed, bal, min_raise):
        srv = self.srv
        up = body.strip().upper().replace(' ', ':')
        if up == 'FOLD':
            if owed <= 0:
                srv.send(p, 'INFO:Nothing to call — folding anyway.')
            return ('fold', 0)
        if up == 'CHECK':
            if owed > 0:
                srv.send(p, 'ERROR:You owe %d — CALL, RAISE:<n>, ALLIN or FOLD.' % owed)
                return None
            return ('call', 0)
        if up == 'CALL':
            return ('call', 0)
        if up == 'ALLIN':
            return ('raise', bal) if bal > owed else ('call', 0)
        if up.startswith('RAISE:') or up.startswith('BET:'):
            try:
                by = int(up.split(':', 1)[1])
                assert by > 0
            except (ValueError, AssertionError):
                srv.send(p, 'ERROR:Use RAISE:<positive integer>')
                return None
            need = owed + by
            if need > bal:
                srv.send(p, 'ERROR:Need %d, you have %d. Use ALLIN to go all-in.' % (need, bal))
                return None
            if by < min_raise and need < bal:
                srv.send(p, 'ERROR:Minimum raise is %d (or ALLIN).' % min_raise)
                return None
            return ('raise', need)
        srv.send(p, 'ERROR:Your turn: FOLD / CHECK / CALL / RAISE:<n> / ALLIN')
        return None

    # ---- showdown -------------------------------------------------------
    def finish(self):
        srv = self.srv
        alive = self.in_hand()
        if len(alive) == 1:
            w = alive[0]
            won = self.pot()
            self.bank[w].add_balance(won)
            msg = 'Player %d wins $%d (last standing).' % (w, won)
            srv.broadcast('WINNER:' + msg)
            log(msg)
            self.t.dlog.write('  ' + msg)
            return

        # deal out any remaining board (everyone all-in)
        while len(self.community) < 5:
            self.community.append(self.t.deck.draw_card())
        srv.broadcast('COMMUNITY:%s' % '|'.join(str(c) for c in self.community))
        srv.broadcast('SHOWDOWN:Revealing hands...')
        scores = {}
        for p in alive:
            scores[p] = evaluate_hand(self.hole[p], self.community)
            srv.broadcast('SHOWDOWN:Player %d shows %s, %s' % (p, *self.hole[p]))
            srv.broadcast('INFO:Player %d has %s.' % (p, HAND_NAMES[scores[p][0]]))

        pots = build_pots(self.contrib, self.folded)
        for i, (amount, eligible) in enumerate(pots):
            best = max(scores[p] for p in eligible)
            winners = [p for p in self.order_from(1) if p in eligible and scores[p] == best]
            share, odd = divmod(amount, len(winners))
            for k, w in enumerate(winners):
                self.bank[w].add_balance(share + (1 if k < odd else 0))
            label = 'main pot' if i == 0 else 'side pot %d' % i
            if len(winners) == 1:
                msg = 'Player %d wins the %s ($%d) with %s!' % (
                    winners[0], label, amount, HAND_NAMES[best[0]])
            else:
                msg = 'Players %s split the %s ($%d) with %s.' % (
                    ', '.join(str(w) for w in winners), label, amount, HAND_NAMES[best[0]])
            srv.broadcast('WINNER:' + msg)
            log(msg)
            self.t.dlog.write('  ' + msg)


# ── Startup ──────────────────────────────────────────────────────────────────
def resolve_seats(n):
    """{uid: player_id} for player0..player<n-1>, looked up before the sandbox."""
    uid_map = {}
    for i in range(n):
        try:
            uid_map[pwd.getpwnam('player%d' % i).pw_uid] = i
        except KeyError:
            log('WARNING: no Unix account player%d — that seat cannot join' % i)
    if not uid_map:
        sys.exit('[dealer] no player accounts found; run poker_install.sh first')
    return uid_map


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    flags = [a for a in sys.argv[1:] if a.startswith('--')]
    if not args or not args[0].isdigit() or not 2 <= int(args[0]) <= 16:
        print('Usage: python3 dealer.py <num_players 2-16> '
              '[--timeout=<s>] [--no-sandbox] [--require-sandbox]')
        sys.exit(1)
    n = int(args[0])
    timeout = ACTION_TIMEOUT
    for f in flags:
        if f.startswith('--timeout='):
            timeout = max(5.0, float(f.split('=', 1)[1]))

    uid_map = resolve_seats(n)
    as_root = os.geteuid() == 0
    owner = pwd.getpwnam('dealer').pw_uid if as_root else -1
    try:
        gid = grp.getgrnam(pc.PLAYER_GROUP).gr_gid
    except KeyError:
        gid = -1

    # Supervisor: create the socket, then hand everything else to the engine.
    srv_sock = pc.create_server_socket(owner_uid=owner, group_gid=gid)
    log('Starting dealer — %d seats on %s' % (n, pc.SOCKET_PATH))

    def engine():
        if as_root:
            sandbox.drop_privileges('dealer')
        dealer_log = SecureLog(pc.DEALER_LOG)   # opened before the filter goes up
        chat_log = SecureLog(pc.CHAT_LOG)
        server = Server(srv_sock, uid_map, chat_log=chat_log)
        if '--no-sandbox' in flags:
            log('WARNING: running WITHOUT the sandbox')
        else:
            applied = sandbox.enter_sandbox(max_fds=n + MAX_EXTRA_FDS,
                                            require='--require-sandbox' in flags)
            log('Engine pid %d sandboxed as uid %d: %s' % (
                os.getpid(), os.getuid(), ', '.join(applied)))
        table = Table(server, sorted(uid_map.values()), dealer_log, timeout)
        try:
            table.run()
        except KeyboardInterrupt:
            log('Interrupted.')
            server.broadcast('GAME_OVER:The dealer shut the table down.')
        finally:
            server.close()
            dealer_log.close()
            chat_log.close()
        return 0

    def cleanup():
        srv_sock.close()
        try:
            os.unlink(pc.SOCKET_PATH)
        except OSError:
            pass
        log('Supervisor: socket removed, exiting.')

    sys.exit(sandbox.supervise(engine, cleanup))


# fds the engine needs beyond one per player: stdio, logs, listening socket,
# pending connections (MAX_PENDING) and some headroom for Python itself.
MAX_EXTRA_FDS = 16 + pc.MAX_PENDING

if __name__ == '__main__':
    main()
