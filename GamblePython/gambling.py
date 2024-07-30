import random
import math
from cards import Deck
from banking import *
import sys
deck = Deck()
balance = BankAccount(1000)

def play_game(play_again):
        print(play_again)
        play = input('> ').lower()
        if play == "no":
            return False
        return True

def leave_game(self):
        print("See you next time!")
        exit(0)


class Blackjack:
    def __init__(self):
        while balance.is_neg() == False:
            deck.shuffle()
            move = "hit"
            print("Your current balance is $", balance.show_balance())
            money = balance.betting_amount("How much money do you want to bet? (Integer value)")
            comp = 0
            player = 0
            while comp < 21 and player < 21:
                if move == "hit" or move == "double":
                    playerDraw = deck.draw_card()
                    print("You drew", playerDraw)
                    player = player + deck.calculate_card(playerDraw.rank)
                    if player <= 21:
                        compDraw = deck.draw_card()
                        print("Dealer draws", compDraw)
                        comp = comp + deck.calculate_card(compDraw.rank)
                elif move == "stand":
                    compDraw = deck.draw_card()
                    print("Dealer draws", compDraw)
                    comp = comp + deck.calculate_card(compDraw.rank)
                    if comp > player and comp < 21:
                        print("Dealer's hand:", comp)
                        break
                print("Dealer's hand:", comp)
                print("Your hand: ", player)
                if (comp < 21 and player < 21) and move != "stand":
                    while True:
                        print("What do you want to do next? (Double/Hit/Stand/Surrender)")
                        move = input('> ').lower()
                        if move in ["double", "hit", "stand", "surrender"]:
                            break
                        else:
                            print("Please input a move in the list")
                    if move == "double":
                        money = money * 2
                    elif move == "surrender":
                        break
            if move == "surrender":
                balance.sub_balance(math.ceil(money / 2))
                print("You surrendered!")
            elif (comp > player and comp <= 21) or comp == 21 or player > 21:
                balance.sub_balance(money)
                print("You lost!")
            elif (player > comp and player <= 21) or player == 21 or comp > 21:
                balance.add_balance(money)
                print("You win!")
            if play_game("Do you wish to play another game?: (Yes/No)") == False:
                break
        if balance.is_neg() == True:
            if play_game("You have run out of money, do you wish to restart? (Yes / No)") == False:
                leave_game()
            else:
                balance.reset_balance()
class Poker:
    def __init__(self):
        while balance.is_neg() == False:
            deck.shuffle()
            print("Your current balance is $", balance.show_balance())
            money = balance.betting_amount("How much money do you want to start in the pot? (Integer value)")
            Turn = random.randint(0, 1)
            if Turn == 0:
                print("Player makes the first move")
            elif Turn == 1:
                print("Computer makes the first move")
            player_rank = 0
            comp_rank = 0
            #Preflop
            player_hand = self.start_hand()
            print("Your hand:")
            deck.create_concealed_hand(player_hand)
            print("Do you wish to peek? (Yes/No)")
            peek = input('> ').lower()
            if peek == "yes":
                deck.create_hand(player_hand)
            comp_hand = self.start_hand()
            #Flop
            while len(player_hand) < 5:
                print("What do you want to do next? (Call/Raise/Fold)")
                move = input('> ')
                if move == "raise":
                    money = money + balance.betting_amount("How much do you want to raise by?")

            print("Your final hand:")
            deck.create_hand(player_hand)
            print("Computer's final hand:")
            deck.create_hand(comp_hand)
            if player_rank > comp_rank:
                print("You win!")
                balance.add_balance(money)
            elif comp_rank > player_rank:
                print("You lost!")
                balance.sub_balance(money)
            elif comp_rank == player_rank:
                print("It's a tie!")
                balance.add_balance(math.ceil(money / 2))
            if play_game("Do you wish to play another game?: (Yes/No)") == False:
                break
        if balance.is_neg() == True:
            if play_game("You have run out of money, do you wish to restart? (Yes / No)") == False:
                leave_game()
            else:
                balance.reset_balance()
    def start_hand(self):
        hand = []
        hand.append(deck.draw_card())
        hand.append(deck.draw_card())
        return hand
    def poker_opponent(self, hand):
        match len(hand):
            case 2:
                return "call"
            case 3:
                return
            case 4:
                return
            case 5:
                return
class Roulette_wheel:
    reds = [str(s) + " Red" for s in [9, 30, 7, 32, 5, 34, 3, 36, 1, 27, 25, 12, 19, 18, 21, 16, 23, 14]]
    blacks = [str(s) + " Black" for s in [28, 26, 11, 20, 17, 22, 15, 24, 13, 10, 29, 8, 31, 6, 33, 4, 35, 2]]
    probability = 7400
    def __init__(self):
        self.roulette_wheel = random.shuffle(self.blacks + self.reds + ["0 Green", "00 Green"])
    def roll_wheel(self):
        landing = random.randint(0, self.probability) % 37
        print("You landed on", self.roulette_wheel[landing])
class Roulette:
    def __init__(self):
        wheel = Roulette_wheel()
        print("Your current balance is $", balance.show_balance())
        money = balance.betting_amount("How much money do you want to bet?")
        self.print_table()
        while money > 0:
            print("What do you want to bet on? ([Dollar Amount] Black/Red/Green/Odds/Evens) Gamble amount left:", money)
            roll = input('> ')
        wheel.roll_wheel()
    def parse_bet(self, bet):
        parseBet = bet.split()
    def roulette_rules(self):
        print("")
    def print_table(self):#prints an ascii visualization of a roulette table, will cover whatever spot the player bets on
        roulette_table = list(range(37))
        twelves_bet = ["1st 12", "2nd 12", "3rd 12"]
        outside_bet = ["1-18", "EVEN", "RED", "BLACK", "ODD", "19-36"]
        #first row
        for i in range(74):
            sys.stdout.write("_")
        sys.stdout.write("\n< 00 | ")
        for i in roulette_table[3::3]:
            sys.stdout.write(str(i) + " | " )
        sys.stdout.write(" 3rd row |\n")
        self.print_table_lines()
        #second row
        sys.stdout.write("|    | ")
        for i in roulette_table[2::3]:
            sys.stdout.write(str(i) + " | ")
        sys.stdout.write(" 2nd row |\n")
        self.print_table_lines()
        #third row
        sys.stdout.write("< 0  | ")
        for i in roulette_table[1::3]:
            sys.stdout.write(str(i) + " | ")
        sys.stdout.write(" 1st row |\n")
        for i in range(74):
            if i in [5, 22, 42, 62]:
                sys.stdout.write("|")
            else:
                sys.stdout.write("‾")
        #fourth row
        sys.stdout.write("\n     |")
        for i in twelves_bet:
            if i == "1st 12":
                sys.stdout.write("    " + i + "      |")
            else:
                sys.stdout.write("      " + i + "       |")
        #fifth row
        sys.stdout.write("\n     ")
        for i in range(58):
            if i in [0, 8, 17, 27, 37, 47, 57]:
                sys.stdout.write("|")
            else:
                sys.stdout.write("‾")
        #sixth row
        sys.stdout.write("\n     |")
        for i in range(6):
            match i:
                case 0:
                    sys.stdout.write(" " + outside_bet[i] + "  |")
                case 1:
                    sys.stdout.write("  " + outside_bet[i] + "  |")
                case 2:
                    sys.stdout.write("   " + outside_bet[i] + "   |")
                case 3:
                    sys.stdout.write("  " + outside_bet[i] + "  |")
                case 4:
                    sys.stdout.write("   " + outside_bet[i] + "   |")
                case 5:
                    sys.stdout.write("  " + outside_bet[i] + "  |")
        #seventh row
        sys.stdout.write("\n     ")
        for i in range(58):
            if i in [0, 8, 17, 27, 37, 47, 57]:
                sys.stdout.write("|")
            else:
                sys.stdout.write("_")
        sys.stdout.write("\n")
    def print_table_lines(self):
        sys.stdout.write("|----")
        #single-digit borders
        for i in range(12):
            if(i % 4 == 0):
                sys.stdout.write("|")
            else:
                sys.stdout.write("-")
        #double-digit borders
        for i in range(46):
            if(i % 5 == 0):
                sys.stdout.write("|")
            else:
                sys.stdout.write("-")
        for i in range(10):
            sys.stdout.write("-")
        print("|")
        
        



