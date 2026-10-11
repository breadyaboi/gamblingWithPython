"""
dealer.py  (Unix domain socket edition)
========================================
Multiplayer Texas Hold'em server.

Usage:
    python3 dealer.py <num_players>
"""

import sys, os, time
sys.path.insert(0, '/home/dealer')

from cards   import Deck, Card
from banking import BankAccount
from poker_communication import (
    SOCKET_PATH,
    create_server_socket, authenticate, cleanup_conn,
    send_to, send_player, recv_from,
    async_recv, broadcast,
    init_dealer_log, log_hands, log_chat,
    render_cards,
)

STARTING_BALANCE = 1000
SMALL_BLIND      = 10
BIG_BLIND        = 20
ACTION_TIMEOUT   = 30.0

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


# ── Hand evaluation ───────────────────────────────────────────────────────────
def _card_value(rank):
    face = {'jack': 11, 'queen': 12, 'king': 13, 'ace': 14}
    v = face.get(rank.strip().lower())
    if v is not None:
        return v
    try:
        return int(rank)
    except ValueError:
        return 0

def _score_five(cards):
    vals  = sorted([_card_value(c.rank) for c in cards], reverse=True)
    suits = [c.suit for c in cards]
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    freq = sorted(counts.values(), reverse=True)
    is_flush = len(set(suits)) == 1
    sv = sorted(set(vals))
    is_straight = len(sv) == 5 and sv[-1] - sv[0] == 4
    if set(sv) == {14, 2, 3, 4, 5}:
        is_straight, vals = True, [5, 4, 3, 2, 1]
    if   is_straight and is_flush: return (8, vals)
    elif freq[0] == 4:             return (7, vals)
    elif freq[:2] == [3, 2]:       return (6, vals)
    elif is_flush:                 return (5, vals)
    elif is_straight:              return (4, vals)
    elif freq[0] == 3:             return (3, vals)
    elif freq[:2] == [2, 2]:       return (2, vals)
    elif freq[0] == 2:             return (1, vals)
    else:                          return (0, vals)

def evaluate_hand(hole, community):
    from itertools import combinations
    best = None
    for five in combinations(hole + community, 5):
        s = _score_five(list(five))
        if best is None or s > best:
            best = s
    return best


# ── Connection phase ──────────────────────────────────────────────────────────
def wait_for_players(srv, num_players):
    log('Waiting for %d players on %s' % (num_players, SOCKET_PATH))
    conn_map, pending = {}, []

    while len(conn_map) < num_players:
        watch = {c: None for c in pending}
        msgs, new_conns = async_recv(watch, server_sock=srv, timeout=2.0)

        for nc in new_conns:
            pending.append(nc)
            send_to(nc, 'IDENTIFY:Send READY:<player_id>')

        for conn in list(pending):
            for sender, body in recv_from(conn):
                if body.upper().startswith('READY:'):
                    try:
                        claimed_id = int(body.split(':', 1)[1])
                    except (ValueError, IndexError):
                        send_to(conn, 'ERROR:Use READY:<number>')
                        continue
                    if not authenticate(conn, claimed_id):
                        send_to(conn, 'ERROR:Auth failed for player%d' % claimed_id)
                        cleanup_conn(conn)
                        pending.remove(conn)
                        break
                    if claimed_id in conn_map.values():
                        send_to(conn, 'ERROR:Player %d already connected' % claimed_id)
                        cleanup_conn(conn)
                        pending.remove(conn)
                        break
                    conn_map[conn] = claimed_id
                    pending.remove(conn)
                    send_to(conn, 'WELCOME:Player %d — balance %d' % (
                        claimed_id, STARTING_BALANCE))
                    log('Player %d connected  (%d/%d)' % (
                        claimed_id, len(conn_map), num_players))
                    break

    return conn_map


# ── Betting round ─────────────────────────────────────────────────────────────
def betting_round(active_pids, conn_map, balances, pot, current_bets,
                  community, round_name):
    max_bet = max(current_bets.values()) if current_bets else BIG_BLIND
    to_act  = list(active_pids)

    active_map = {c: p for c, p in conn_map.items() if p in active_pids}
    broadcast('PHASE:%s' % round_name, active_map)
    broadcast('POT:%d'   % pot,        active_map)
    if community:
        broadcast('COMMUNITY:%s' % '|'.join(str(c) for c in community), active_map)
    for pid in active_pids:
        owed = max_bet - current_bets.get(pid, 0)
        send_player(pid, 'YOUR_TURN:to_call=%d balance=%d' % (
            owed, balances[pid].show_balance()), conn_map)

    log('%s  pot=$%d' % (round_name, pot))

    while to_act:
        msgs, _ = async_recv(active_map, timeout=ACTION_TIMEOUT)
        if not msgs:
            for p in list(to_act):
                active_pids.remove(p)
                to_act.remove(p)
                broadcast('INFO:Player %d timed out (auto-fold).' % p,
                          {c: pid for c, pid in conn_map.items()
                           if pid in active_pids})
                log('Player %d auto-folded (timeout)' % p)
            break

        for player, body in msgs:
            if player not in to_act:
                continue
            upper = body.strip().upper()

            if upper == 'FOLD':
                to_act.remove(player)
                active_pids.remove(player)
                send_player(player, 'ACK:You folded.', conn_map)
                remain = {c: p for c, p in conn_map.items() if p in active_pids}
                broadcast('INFO:Player %d folded.' % player, remain)
                log('Player %d FOLD' % player)

            elif upper == 'CALL':
                owed   = max_bet - current_bets.get(player, 0)
                amount = min(owed, balances[player].show_balance())
                balances[player].sub_balance(amount)
                pot += amount
                current_bets[player] = current_bets.get(player, 0) + amount
                to_act.remove(player)
                send_player(player, 'ACK:Called %d. Balance: %d' % (
                    amount, balances[player].show_balance()), conn_map)
                log('Player %d CALL $%d' % (player, amount))

            elif upper.startswith('RAISE:'):
                try:
                    raise_by = int(body.strip().split(':', 1)[1])
                    assert raise_by > 0
                except (ValueError, IndexError, AssertionError):
                    send_player(player, 'ERROR:Use RAISE:<positive integer>', conn_map)
                    continue
                needed = (max_bet - current_bets.get(player, 0)) + raise_by
                if balances[player].show_balance() < needed:
                    send_player(player, 'ERROR:Need %d have %d' % (
                        needed, balances[player].show_balance()), conn_map)
                    continue
                balances[player].sub_balance(needed)
                pot    += needed
                max_bet += raise_by
                current_bets[player] = max_bet
                to_act.remove(player)
                for p in active_pids:
                    if p != player and p not in to_act:
                        to_act.append(p)
                send_player(player, 'ACK:Raised %d. Max=%d. Balance: %d' % (
                    raise_by, max_bet, balances[player].show_balance()), conn_map)
                remain = {c: p for c, p in conn_map.items() if p in active_pids}
                broadcast('INFO:Player %d raised to %d.' % (player, max_bet),
                          {c: p for c, p in remain.items() if p != player})
                log('Player %d RAISE $%d  (max=%d)' % (player, raise_by, max_bet))

            elif upper.startswith('CHAT:'):
                text = body.strip()[5:]
                log_chat(player, text)
                all_map = {c: p for c, p in conn_map.items() if p in active_pids}
                broadcast('CHAT:[P%d] %s' % (player, text), all_map)
                log('Player %d CHAT: %s' % (player, text))

            else:
                send_player(player, 'ERROR:Unknown action. '
                            'FOLD / CALL / RAISE:<n> / CHAT:<text>', conn_map)

    return pot, active_pids


# ── Single hand ───────────────────────────────────────────────────────────────
def play_hand(players, conn_map, balances, deck, hand_num):
    deck.shuffle()
    log('=== Hand #%d ===' % hand_num)

    # Deal hole cards
    hands = {}
    for p in players:
        c1, c2 = deck.draw_card(), deck.draw_card()
        hands[p] = [c1, c2]
        send_player(p, 'HOLE:%s|%s' % (c1, c2), conn_map)

    log_hands(hands, hand_num)
    log('Hole cards dealt to %d players' % len(players))
    for p in players:
        log_cards('P%d:' % p, hands[p])

    # Blinds
    pot = 0
    current_bets = {p: 0 for p in players}
    if len(players) >= 2:
        sb, bb = players[0], players[1]
        balances[sb].sub_balance(SMALL_BLIND)
        balances[bb].sub_balance(BIG_BLIND)
        current_bets[sb] = SMALL_BLIND
        current_bets[bb] = BIG_BLIND
        pot = SMALL_BLIND + BIG_BLIND
        send_player(sb, 'INFO:You posted small blind (%d).' % SMALL_BLIND, conn_map)
        send_player(bb, 'INFO:You posted big blind (%d).'  % BIG_BLIND,   conn_map)
        log('Blinds: P%d=$%d  P%d=$%d  Pot=$%d' % (sb, SMALL_BLIND, bb, BIG_BLIND, pot))

    active = list(players)

    # Pre-flop
    pot, active = betting_round(active, conn_map, balances, pot,
                                dict(current_bets), [], 'pre-flop')
    if len(active) <= 1:
        return _end_hand(active, hands, [], balances, pot, conn_map)

    # Flop
    community = [deck.draw_card() for _ in range(3)]
    log_cards('FLOP:', community)
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active}, list(community), 'flop')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, conn_map)

    # Turn
    community.append(deck.draw_card())
    log_cards('TURN:', community)
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active}, list(community), 'turn')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, conn_map)

    # River
    community.append(deck.draw_card())
    log_cards('RIVER:', community)
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active}, list(community), 'river')

    return _end_hand(active, hands, community, balances, pot, conn_map)


def _end_hand(active, hands, community, balances, pot, conn_map):
    all_pids = list(hands.keys())
    all_map  = {c: p for c, p in conn_map.items() if p in all_pids}

    if len(active) == 0:
        broadcast('INFO:No winner.', all_map)
        log('No winner (all folded simultaneously)')

    elif len(active) == 1:
        w = active[0]
        balances[w].add_balance(pot)
        msg = 'Player %d wins $%d (last standing).' % (w, pot)
        broadcast('WINNER:' + msg, all_map)
        log(msg)

    else:
        broadcast('SHOWDOWN:Revealing hands...', all_map)
        for p in active:
            h = hands[p]
            broadcast('SHOWDOWN:Player %d shows %s, %s' % (p, h[0], h[1]), all_map)
        best, winner = None, None
        for p in active:
            s = evaluate_hand(hands[p], community)
            if best is None or s > best:
                best, winner = s, p
        name = HAND_NAMES[best[0]] if best else '?'
        balances[winner].add_balance(pot)
        msg = 'Player %d wins $%d with %s!' % (winner, pot, name)
        broadcast('WINNER:' + msg, all_map)
        log(msg)

    return balances


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Usage: python3 dealer.py <num_players>')
        sys.exit(1)

    num_players = int(sys.argv[1])
    log('Starting dealer — %d players  (%s)' % (num_players, SOCKET_PATH))

    init_dealer_log()

    srv      = create_server_socket()
    conn_map = wait_for_players(srv, num_players)
    players  = sorted(conn_map.values())
    balances = {p: BankAccount(STARTING_BALANCE) for p in players}

    all_map = dict(conn_map)
    broadcast('GAME_START:Game on! Starting balance: %d chips.' % STARTING_BALANCE,
              all_map)
    log('All players connected — game starting')

    deck     = Deck()
    hand_num = 0

    try:
        while True:
            active = [p for p in players if balances[p].show_balance() > 0]
            if len(active) < 2:
                msg = ('Player %d wins the game!' % active[0]
                       if active else 'No chips remain.')
                broadcast('GAME_OVER:' + msg, all_map)
                log('GAME OVER: ' + msg)
                break

            hand_num += 1
            broadcast('NEW_HAND:#%d — %d players active.' % (hand_num, len(active)),
                      all_map)
            for p in active:
                send_player(p, 'BALANCE:%d' % balances[p].show_balance(), conn_map)

            balances = play_hand(active, conn_map, balances, deck, hand_num)
            deck.reset_deck()
            time.sleep(2)
    finally:
        for conn in conn_map:
            cleanup_conn(conn)
        srv.close()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        print('\n[dealer] Game over after %d hands.' % hand_num)


if __name__ == '__main__':
    main()