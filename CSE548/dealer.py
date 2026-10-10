"""
dealer.py  (Unix domain socket edition)
========================================
Multiplayer Texas Hold'em server.  Run as the `dealer` account.

Usage:
    python3 dealer.py <num_players>

The dealer's entire event loop is built on one select() call that
simultaneously watches:

    server_sock          → new player connections (accept)
    client_socks[0..N]   → player messages        (recv)

No threads, no blocking per-player loops.

Attackable surfaces
-------------------
  1. Impersonation      — connect to game.sock before player 0 does,
                          send READY:0, receive their hole cards
                          (fixed by SO_PEERCRED in readVulnerability=0)

  2. Command injection  — CHAT:text; shell_cmd
                          executes shell_cmd as the dealer user

  3. Log race condition — game.sock world-accessible briefly at startup;
                          dealer.log world-readable between touch/chmod

  4. Timing side channel — fold response is ~5× faster than raise;
                           an attacker who measures socket round-trip
                           time per action can infer hand strength
"""

import sys, os, time
sys.path.insert(0, '/home/dealer')

from cards   import Deck
from banking import BankAccount
from poker_communication import (
    SOCKET_PATH,
    create_server_socket, authenticate, cleanup_conn,
    send_to, send_player, recv_from,
    async_recv, broadcast,
    init_dealer_log, log_hands, log_chat,
)

STARTING_BALANCE = 1000
SMALL_BLIND      = 10
BIG_BLIND        = 20
ACTION_TIMEOUT   = 30.0

HAND_NAMES = [
    'High Card', 'One Pair', 'Two Pair', 'Three of a Kind',
    'Straight', 'Flush', 'Full House', 'Four of a Kind', 'Straight Flush',
]


# ── Hand evaluation ──────────────────────────────────────────────────────────
def _card_value(rank):
    face = {'Jack': 11, 'Queen': 12, 'King': 13, 'Ace': 14}
    return face.get(rank, int(rank))

def _score_five(cards):
    vals  = sorted([_card_value(c.rank) for c in cards], reverse=True)
    suits = [c.suit for c in cards]
    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    freq = sorted(counts.values(), reverse=True)
    is_flush    = len(set(suits)) == 1
    sv          = sorted(set(vals))
    is_straight = len(sv) == 5 and sv[-1] - sv[0] == 4
    if set(sv) == {14, 2, 3, 4, 5}:        # wheel
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


# ── Connection phase ─────────────────────────────────────────────────────────
def wait_for_players(srv, num_players):
    """
    Accept connections until *num_players* players have authenticated.

    Uses async_recv() with server_sock so new connections and early
    player messages are handled in the same select() call.

    Returns conn_map: { socket_obj : player_id }
    """
    print('[dealer] Listening on %s' % SOCKET_PATH)
    print('[dealer] Waiting for %d players...' % num_players)

    conn_map     = {}    # {conn: player_id}  — authenticated players
    pending      = []    # connections that have connected but not yet sent READY

    while len(conn_map) < num_players:
        # Watch: server sock for new connects, pending conns for READY msgs
        watch = {c: None for c in pending}
        msgs, new_conns = async_recv(watch, server_sock=srv, timeout=2.0)

        # Accept newly connected sockets into pending
        for nc in new_conns:
            pending.append(nc)
            # Ask for identification
            send_to(nc, 'IDENTIFY:Send READY:<player_id>')

        # Process READY messages from pending connections
        for conn in list(pending):
            for sender, body in recv_from(conn):
                if body.upper().startswith('READY:'):
                    try:
                        claimed_id = int(body.split(':', 1)[1])
                    except (ValueError, IndexError):
                        send_to(conn, 'ERROR:Bad format. Send READY:<number>')
                        continue

                    # ── AUTHENTICATION ─────────────────────────────────────
                    # readVulnerability=1: trusts claimed_id without checking
                    # readVulnerability=0: verifies via SO_PEERCRED
                    if not authenticate(conn, claimed_id):
                        send_to(conn, 'ERROR:Authentication failed. '
                                      'You are not player%d.' % claimed_id)
                        cleanup_conn(conn)
                        pending.remove(conn)
                        break

                    if claimed_id in conn_map.values():
                        send_to(conn, 'ERROR:Player %d already connected.' % claimed_id)
                        cleanup_conn(conn)
                        pending.remove(conn)
                        break

                    conn_map[conn] = claimed_id
                    pending.remove(conn)
                    send_to(conn, 'WELCOME:Player %d connected. Balance: %d' % (
                        claimed_id, STARTING_BALANCE))
                    print('[dealer] Player %d connected (%d/%d).' % (
                        claimed_id, len(conn_map), num_players))
                    break

    print('[dealer] All players connected.')
    return conn_map


# ── Betting round ─────────────────────────────────────────────────────────────
def betting_round(active_pids, conn_map, balances, pot, current_bets,
                  community=None, round_name=''):
    """
    One async betting round using select() over all player sockets.

    TIMING SIDE CHANNEL:
        FOLD  → O(1) list removal                  fast  (~0.1 ms)
        CALL  → BankAccount read + write           medium
        RAISE → BankAccount arithmetic + re-queue  slow  (~0.5 ms)
    An attacker timing the gap between sending an action and receiving
    the dealer's acknowledgement can distinguish these paths.
    """
    max_bet  = max(current_bets.values()) if current_bets else BIG_BLIND
    to_act   = list(active_pids)

    # Announce round to active players
    active_map = {c: p for c, p in conn_map.items() if p in active_pids}
    broadcast('PHASE:%s' % round_name, active_map)
    broadcast('POT:%d'   % pot,        active_map)
    if community:
        broadcast('COMMUNITY:%s' % ','.join(str(c) for c in community),
                  active_map)
    for pid in active_pids:
        owed = max_bet - current_bets.get(pid, 0)
        send_player(pid, 'YOUR_TURN:to_call=%d balance=%d' % (
            owed, balances[pid].show_balance()), conn_map)

    while to_act:
        msgs, _ = async_recv(active_map, timeout=ACTION_TIMEOUT)

        if not msgs:
            for p in list(to_act):
                active_pids.remove(p)
                to_act.remove(p)
                broadcast('INFO:Player %d timed out (auto-fold).' % p,
                          {c: pid for c, pid in conn_map.items()
                           if pid in active_pids})
            break

        for player, body in msgs:
            if player not in to_act:
                continue

            upper = body.strip().upper()

            if upper == 'FOLD':
                # ── FAST ──────────────────────────────────────────────────
                to_act.remove(player)
                active_pids.remove(player)
                send_player(player, 'ACK:You folded.', conn_map)
                remain = {c: p for c, p in conn_map.items() if p in active_pids}
                broadcast('INFO:Player %d folded.' % player, remain)

            elif upper == 'CALL':
                # ── MEDIUM ────────────────────────────────────────────────
                owed   = max_bet - current_bets.get(player, 0)
                amount = min(owed, balances[player].show_balance())
                balances[player].sub_balance(amount)
                pot += amount
                current_bets[player] = current_bets.get(player, 0) + amount
                to_act.remove(player)
                send_player(player,
                    'ACK:Called %d. Balance: %d' % (
                        amount, balances[player].show_balance()),
                    conn_map)

            elif upper.startswith('RAISE:'):
                # ── SLOW ──────────────────────────────────────────────────
                try:
                    raise_by = int(body.strip().split(':', 1)[1])
                    if raise_by <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    send_player(player, 'ERROR:Use RAISE:<positive integer>',
                                conn_map)
                    continue
                needed = (max_bet - current_bets.get(player, 0)) + raise_by
                if balances[player].show_balance() < needed:
                    send_player(player,
                        'ERROR:Need %d, have %d.' % (
                            needed, balances[player].show_balance()),
                        conn_map)
                    continue
                balances[player].sub_balance(needed)
                pot      += needed
                max_bet  += raise_by
                current_bets[player] = max_bet
                to_act.remove(player)
                for p in active_pids:
                    if p != player and p not in to_act:
                        to_act.append(p)
                send_player(player,
                    'ACK:Raised by %d. Max bet now %d. Balance: %d' % (
                        raise_by, max_bet, balances[player].show_balance()),
                    conn_map)
                remain = {c: p for c, p in conn_map.items() if p in active_pids}
                broadcast('INFO:Player %d raised to %d.' % (player, max_bet),
                          {c: p for c, p in remain.items() if p != player})

            elif upper.startswith('CHAT:'):
                # ── COMMAND INJECTION target ───────────────────────────────
                text = body.strip()[5:]      # strip "CHAT:"
                log_chat(player, text)       # ← injection here when vuln=1
                all_map = {c: p for c, p in conn_map.items()
                           if p in active_pids}
                broadcast('CHAT:[Player %d] %s' % (player, text), all_map)
                # chatting doesn't use your action; to_act unchanged

            else:
                send_player(player,
                    'ERROR:Unknown action. '
                    'Use FOLD / CALL / RAISE:<n> / CHAT:<text>',
                    conn_map)

    return pot, active_pids


# ── Single hand ───────────────────────────────────────────────────────────────
def play_hand(players, conn_map, balances, deck, hand_num):
    deck.shuffle()

    # ── Deal hole cards ───────────────────────────────────────────────────
    hands = {}
    for p in players:
        c1, c2 = deck.draw_card(), deck.draw_card()
        hands[p] = [c1, c2]
        send_player(p, 'HOLE:%s|%s' % (c1, c2), conn_map)

    # ── Log ALL hands (the secret students want to steal) ─────────────────
    log_hands(hands, hand_num)

    # ── Blinds ────────────────────────────────────────────────────────────
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

    active = list(players)

    # ── Pre-flop ──────────────────────────────────────────────────────────
    pot, active = betting_round(active, conn_map, balances, pot,
                                dict(current_bets), round_name='pre-flop')
    if len(active) <= 1:
        return _end_hand(active, hands, [], balances, pot, conn_map)

    # ── Flop ──────────────────────────────────────────────────────────────
    community = [deck.draw_card() for _ in range(3)]
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='flop')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, conn_map)

    # ── Turn ──────────────────────────────────────────────────────────────
    community.append(deck.draw_card())
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='turn')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, conn_map)

    # ── River ─────────────────────────────────────────────────────────────
    community.append(deck.draw_card())
    pot, active = betting_round(active, conn_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='river')

    return _end_hand(active, hands, community, balances, pot, conn_map)


def _end_hand(active, hands, community, balances, pot, conn_map):
    all_pids = list(hands.keys())
    all_map  = {c: p for c, p in conn_map.items() if p in all_pids}

    if len(active) == 0:
        broadcast('INFO:No winner.', all_map)

    elif len(active) == 1:
        w = active[0]
        balances[w].add_balance(pot)
        broadcast('WINNER:Player %d wins %d (last standing).' % (w, pot), all_map)

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
        broadcast('WINNER:Player %d wins %d with %s!' % (winner, pot, name), all_map)

    return balances


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Usage: python3 dealer.py <num_players>')
        sys.exit(1)

    num_players = int(sys.argv[1])
    print('[dealer] Starting — %d players, socket: %s' % (num_players, SOCKET_PATH))

    init_dealer_log()          # creates dealer.log (race window if vuln=1)

    srv      = create_server_socket()
    conn_map = wait_for_players(srv, num_players)
    players  = sorted(conn_map.values())
    balances = {p: BankAccount(STARTING_BALANCE) for p in players}

    all_map = dict(conn_map)
    broadcast('GAME_START:Game on!  Starting balance: %d chips.' % STARTING_BALANCE,
              all_map)

    deck     = Deck()
    hand_num = 0

    try:
        while True:
            active = [p for p in players if balances[p].show_balance() > 0]
            if len(active) < 2:
                if active:
                    broadcast('GAME_OVER:Player %d wins!' % active[0], all_map)
                else:
                    broadcast('GAME_OVER:No chips remain.', all_map)
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
        print('[dealer] Game over after %d hands.' % hand_num)


if __name__ == '__main__':
    main()
