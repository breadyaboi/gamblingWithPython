"""
dealer.py
=========
Multiplayer Texas Hold'em poker server.
Run as the `dealer` system account.

Usage:
    python3 dealer.py <num_players>

Game flow (one hand):
    1. Deal 2 hole cards to each player
    2. Log ALL hands to dealer.log  ← the secret students want to steal
    3. Pre-flop betting round  (select-based async I/O)
    4. Flop  (3 community cards) + betting round
    5. Turn  (1 community card)  + betting round
    6. River (1 community card)  + betting round
    7. Showdown — evaluate best 5-card hand from 7 available

Player actions during a betting round:
    FOLD             — leave this hand
    CALL             — match the current bet
    RAISE:<amount>   — raise by <amount> chips (e.g.  RAISE:50)
    CHAT:<text>      — send a message to all players
                       ↑ command injection via readVulnerability_2=1

Attackable surfaces (mirror of Virtual Werewolves):
    • ps aux          — cat PIDs reveal which player pipe dealer is reading
    • FIFO wait state — fold/call/raise produce measurably different
                        select() loop delays (timing side channel)
    • Command inject  — CHAT:<shell code> runs as the dealer user
    • Race condition  — log file briefly world-readable on startup
"""

import sys, os, time
sys.path.insert(0, '/home/dealer')

from cards import Deck
from banking import BankAccount
from poker_communication import (
    send, broadcast, recv,
    async_recv, open_player_pipes, close_pipes,
    init_dealer_log, log_hands,
)

STARTING_BALANCE = 1000
SMALL_BLIND      = 10
BIG_BLIND        = 20
ACTION_TIMEOUT   = 30.0   # seconds a player has to act before auto-fold

# Straight-flush → high card
HAND_NAMES = [
    'High Card', 'One Pair', 'Two Pair', 'Three of a Kind',
    'Straight',  'Flush',    'Full House', 'Four of a Kind',
    'Straight Flush',
]


# ── Hand evaluation ──────────────────────────────────────────────────────────
def _card_value(rank):
    """Numeric value of a rank string (Ace=14 for comparison)."""
    face = {'Jack': 11, 'Queen': 12, 'King': 13, 'Ace': 14}
    return face.get(rank, int(rank))


def evaluate_hand(hole_cards, community):
    """
    Return a comparable tuple (rank_index, [tiebreak values]) for the
    best 5-card hand constructible from *hole_cards* + *community*.

    This is a simplified evaluator sufficient for demo purposes;
    it does not handle all suit-based tiebreaks.
    """
    from itertools import combinations
    all_cards = hole_cards + community
    best = None

    for five in combinations(all_cards, 5):
        score = _score_five(list(five))
        if best is None or score > best:
            best = score
    return best


def _score_five(cards):
    vals  = sorted([_card_value(c.rank) for c in cards], reverse=True)
    suits = [c.suit for c in cards]

    counts = {}
    for v in vals:
        counts[v] = counts.get(v, 0) + 1
    freq = sorted(counts.values(), reverse=True)   # e.g. [3,1,1] for trips

    is_flush    = len(set(suits)) == 1
    sorted_vals = sorted(set(vals))
    is_straight = (
        len(sorted_vals) == 5 and sorted_vals[-1] - sorted_vals[0] == 4
    )
    # Wheel (A-2-3-4-5)
    if set(sorted_vals) == {14, 2, 3, 4, 5}:
        is_straight = True
        vals = [5, 4, 3, 2, 1]

    if   is_straight and is_flush: return (8, vals)
    elif freq[0] == 4:             return (7, vals)
    elif freq[:2] == [3, 2]:       return (6, vals)
    elif is_flush:                 return (5, vals)
    elif is_straight:              return (4, vals)
    elif freq[0] == 3:             return (3, vals)
    elif freq[:2] == [2, 2]:       return (2, vals)
    elif freq[0] == 2:             return (1, vals)
    else:                          return (0, vals)


# ── Connection handshake ─────────────────────────────────────────────────────
def wait_for_players(num_players):
    """
    Block until *num_players* players have sent the READY message.
    Returns the list of connected player IDs (0-indexed integers).

    Uses the blocking recv() helper (which itself uses os.popen cat when
    readVulnerability=1, making each wait visible in ps aux).
    """
    print('[dealer] Waiting for %d players to connect...' % num_players)
    connected = []
    # We wait for each player sequentially here; once all are connected
    # the main loop switches to async select()-based I/O.
    for n in range(num_players):
        print('[dealer] Waiting for player %d...' % n)
        data = recv(n)                      # blocking — spawns cat if vuln=1
        if 'READY' in data:
            connected.append(n)
            send(n, 'WELCOME:You are Player %d. Balance: %d' % (n, STARTING_BALANCE))
            print('[dealer] Player %d connected.' % n)
    return connected


# ── Betting round ─────────────────────────────────────────────────────────────
def betting_round(active, fd_map, balances, pot, current_bets,
                  community=None, round_name=''):
    """
    One asynchronous betting round.

    *active*       list of player IDs still in the hand
    *fd_map*       { fd : player_id } for select()
    *balances*     { player_id : BankAccount }
    *pot*          current pot size (int)
    *current_bets* { player_id : chips_committed_this_round }

    Returns (pot, active_players_remaining).

    ── TIMING SIDE CHANNEL ────────────────────────────────────────────────
    Processing time after a message arrives on a pipe differs by action:

      FOLD  → pop from list, no arithmetic  → fast  (~0.1 ms)
      CALL  → BankAccount.show_balance() + sub_balance()  → slightly slower
      RAISE → BankAccount arithmetic + re-queue all active players → slowest

    An attacker watching the FIFO open()-wait-state duration (e.g., the
    hold-and-probe technique from Virtual Werewolves) can distinguish these
    response times and infer which players have strong/weak hands.
    ──────────────────────────────────────────────────────────────────────
    """
    max_bet   = max(current_bets.values()) if current_bets else BIG_BLIND
    to_act    = list(active)               # players who still need to act

    # Notify players
    broadcast('PHASE:%s' % round_name, {p: None for p in active})
    broadcast('POT:%d'   % pot,         {p: None for p in active})
    if community:
        broadcast('COMMUNITY:%s' % ','.join(str(c) for c in community),
                  {p: None for p in active})
    for p in active:
        owed = max_bet - current_bets.get(p, 0)
        send(p, 'YOUR_TURN:to_call=%d balance=%d' % (
            owed, balances[p].show_balance()))

    while to_act:
        results = async_recv(fd_map, timeout=ACTION_TIMEOUT)

        if not results:
            # Timeout — auto-fold all remaining
            for p in list(to_act):
                active.remove(p)
                to_act.remove(p)
                broadcast('INFO:Player %d timed out and folded' % p,
                          {pl: None for pl in active})
            break

        for player, msg in results:
            if player not in to_act:
                continue

            msg = msg.strip()
            upper = msg.upper()

            if upper == 'FOLD':
                # ── FAST path (timing side channel) ──────────────────────
                to_act.remove(player)
                active.remove(player)
                send(player, 'ACK:You folded.')
                broadcast('INFO:Player %d folded.' % player,
                          {p: None for p in active})

            elif upper == 'CALL':
                # ── MEDIUM path ───────────────────────────────────────────
                owed = max_bet - current_bets.get(player, 0)
                avail = balances[player].show_balance()
                amount = min(owed, avail)          # handles all-in
                balances[player].sub_balance(amount)
                pot += amount
                current_bets[player] = current_bets.get(player, 0) + amount
                to_act.remove(player)
                send(player, 'ACK:Called %d. Balance: %d' % (
                    amount, balances[player].show_balance()))

            elif upper.startswith('RAISE:'):
                # ── SLOW path ─────────────────────────────────────────────
                try:
                    raise_by = int(msg.split(':', 1)[1])
                    if raise_by <= 0:
                        raise ValueError
                except (ValueError, IndexError):
                    send(player, 'ERROR:Use RAISE:<positive integer>')
                    continue

                total_needed = (max_bet - current_bets.get(player, 0)) + raise_by
                if balances[player].show_balance() < total_needed:
                    send(player, 'ERROR:Insufficient funds (need %d have %d)' % (
                        total_needed, balances[player].show_balance()))
                    continue

                balances[player].sub_balance(total_needed)
                pot += total_needed
                max_bet += raise_by
                current_bets[player] = max_bet
                to_act.remove(player)
                # Re-add everyone else so they can respond to the raise
                for p in active:
                    if p != player and p not in to_act:
                        to_act.append(p)

                send(player, 'ACK:Raised by %d. Max bet now %d. Balance: %d' % (
                    raise_by, max_bet, balances[player].show_balance()))
                broadcast('INFO:Player %d raised to %d.' % (player, max_bet),
                          {p: None for p in active if p != player})

            elif upper.startswith('CHAT:'):
                # ── COMMAND INJECTION target ───────────────────────────────
                # The message is forwarded through send(), which (when
                # readVulnerability_2=1) builds:
                #   echo :d:<player>: <text> > <pipe>
                # and passes it to os.popen().  If <text> contains
                # shell metacharacters the dealer executes them.
                # Example: CHAT:hello; cat /home/dealer/log/dealer.log > /tmp/x
                chat_text = msg[5:]   # strip "CHAT:"
                broadcast('CHAT:[Player %d]: %s' % (player, chat_text),
                          {p: None for p in active})
                # to_act unchanged — chatting doesn't use your action

            else:
                send(player, 'ERROR:Unknown action. Use FOLD / CALL / RAISE:<n> / CHAT:<text>')

    return pot, active


# ── Single hand ──────────────────────────────────────────────────────────────
def play_hand(players, balances, deck, hand_num):
    """Deal and play one complete hand. Returns updated *balances*."""
    deck.shuffle()

    # Open all player→dealer pipes once; keep them open for the entire hand
    fd_map = open_player_pipes(players)

    # ── Deal hole cards ───────────────────────────────────────────────────
    hands = {}
    for p in players:
        c1, c2 = deck.draw_card(), deck.draw_card()
        hands[p] = [c1, c2]
        send(p, 'HOLE:%s|%s' % (c1, c2))

    # ── Log ALL hands to dealer.log (the target for attacks) ─────────────
    log_hands(hands, hand_num)

    # ── Post blinds ───────────────────────────────────────────────────────
    pot = 0
    current_bets = {p: 0 for p in players}
    if len(players) >= 2:
        sb, bb = players[0], players[1]
        balances[sb].sub_balance(SMALL_BLIND)
        balances[bb].sub_balance(BIG_BLIND)
        current_bets[sb] = SMALL_BLIND
        current_bets[bb] = BIG_BLIND
        pot = SMALL_BLIND + BIG_BLIND
        send(sb, 'INFO:You posted the small blind (%d).' % SMALL_BLIND)
        send(bb, 'INFO:You posted the big blind (%d).'  % BIG_BLIND)

    active = list(players)

    # ── Pre-flop ─────────────────────────────────────────────────────────
    pot, active = betting_round(active, fd_map, balances, pot,
                                dict(current_bets), round_name='pre-flop')
    if len(active) <= 1:
        return _end_hand(active, hands, [], balances, pot, fd_map)

    # ── Flop ──────────────────────────────────────────────────────────────
    community = [deck.draw_card() for _ in range(3)]
    pot, active = betting_round(active, fd_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='flop')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, fd_map)

    # ── Turn ──────────────────────────────────────────────────────────────
    community.append(deck.draw_card())
    pot, active = betting_round(active, fd_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='turn')
    if len(active) <= 1:
        return _end_hand(active, hands, community, balances, pot, fd_map)

    # ── River ─────────────────────────────────────────────────────────────
    community.append(deck.draw_card())
    pot, active = betting_round(active, fd_map, balances, pot,
                                {p: 0 for p in active},
                                community=list(community), round_name='river')

    return _end_hand(active, hands, community, balances, pot, fd_map)


def _end_hand(active, hands, community, balances, pot, fd_map):
    """Determine winner, award pot, reveal hands."""
    close_pipes(fd_map)
    all_players = list(hands.keys())

    if len(active) == 0:
        broadcast('INFO:No winner (all folded simultaneously?).',
                  {p: None for p in all_players})

    elif len(active) == 1:
        winner = active[0]
        balances[winner].add_balance(pot)
        broadcast(
            'WINNER:Player %d wins %d (last player standing).' % (winner, pot),
            {p: None for p in all_players})

    else:
        # Showdown
        broadcast('SHOWDOWN:Revealing hands...', {p: None for p in all_players})
        for p in active:
            h = hands[p]
            broadcast('SHOWDOWN:Player %d shows %s, %s' % (p, h[0], h[1]),
                      {pl: None for pl in all_players})

        best_score, winner = None, None
        for p in active:
            score = evaluate_hand(hands[p], community)
            if best_score is None or score > best_score:
                best_score, winner = score, p

        hand_name = HAND_NAMES[best_score[0]] if best_score else '?'
        balances[winner].add_balance(pot)
        broadcast(
            'WINNER:Player %d wins %d with %s!' % (winner, pot, hand_name),
            {p: None for p in all_players})

    return balances


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print('Usage: python3 dealer.py <num_players>')
        sys.exit(1)

    num_players = int(sys.argv[1])
    print('[dealer] Starting poker game — %d players' % num_players)

    # Create dealer log (race condition if dealerLogMode=1)
    init_dealer_log()

    # Block until all players are connected
    players = wait_for_players(num_players)
    balances = {p: BankAccount(STARTING_BALANCE) for p in players}

    broadcast('GAME_START:Game on!  Starting balance: %d chips each.' % STARTING_BALANCE,
              {p: None for p in players})

    deck     = Deck()
    hand_num = 0

    while True:
        # Remove busted players
        active = [p for p in players if balances[p].show_balance() > 0]
        if len(active) < 2:
            if active:
                broadcast('GAME_OVER:Player %d wins the game!' % active[0],
                          {p: None for p in players})
            else:
                broadcast('GAME_OVER:No chips remain.', {p: None for p in players})
            break

        hand_num += 1
        broadcast('NEW_HAND:#%d — %d players remaining.' % (hand_num, len(active)),
                  {p: None for p in active})
        for p in active:
            send(p, 'BALANCE:%d' % balances[p].show_balance())

        balances = play_hand(active, balances, deck, hand_num)
        deck.reset_deck()
        time.sleep(2)

    print('[dealer] Game over after %d hands.' % hand_num)


if __name__ == '__main__':
    main()
