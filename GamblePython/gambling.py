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
    red_nums = [9, 30, 7, 32, 5, 34, 3, 36, 1, 27, 25, 12, 19, 18, 21, 16, 23, 14]
    black_nums = [28, 26, 11, 20, 17, 22, 15, 24, 13, 10, 29, 8, 31, 6, 33, 4, 35, 2]
    green_nums = ["0", "00"]
    greens = [s + " Green" for s in green_nums]
    reds = [str(s) + " Red" for s in red_nums]
    blacks = [str(s) + " Black" for s in black_nums]
    probability = 7400
    def __init__(self):
        self.roulette_wheel = self.blacks + self.reds + self.greens
        random.shuffle(self.roulette_wheel)
    def roll_wheel(self):
        landing = random.randint(0, self.probability) % 37 #represents the number of times the ball would hypothetically spin around the wheel
        return self.roulette_wheel[landing]
    def black_rolls(self):
        return self.black_nums
    def red_rolls(self):
        return self.red_nums
    def green_rolls(self):
        return self.green_nums
class Roulette:
    bets = []
    table_values = []
    wheel = Roulette_wheel()
    def __init__(self):
        self.roulette_rules()
        while balance.is_neg() == False:
            print("Your current balance is $", balance.show_balance())
            money = balance.betting_amount("How much money do you want to bet?")
            self.table_values = self.create_table_values()
            self.bets = []
            bet_money = []
            roulette_table(self.table_values, self.bets)
            bet_amount = 2
            while money > 0 and bet_amount > 0:
                print("What do you want to bet on? Gamble amount left:", money, "\nBets left:", bet_amount)
                roll = input('> ').lower()
                if self.parse_bet(roll) == True:
                    roll = roll.split()
                    money = money - int(roll[0])
                    bet_money.append(roll[0])
                    bet_amount = bet_amount - 1
                    print(self.bets)
                    print(bet_money)
                    roulette_table(self.table_values, self.bets)
                else:
                    print("Please follow the syntax listed above")
            landed = self.wheel.roll_wheel()
            print(landed)
            self.calculate_bets(landed, bet_money)
            if play_game("Do you wish to play another game?: (Yes/No)") == False:
                break
    def parse_bet(self, roll):#parses the users input
        if self.verify_bet(roll) == True:
            checkInt = roll.split()
            checkInt = checkInt[1:]
            check_bet = ' '.join(checkInt)
            rolls = check_bet.split(", ")
            if len(rolls) > 1:
                    inside_bets = []
                    for i in range(len(rolls)):
                        if i == 6:
                            break
                        else:
                            if rolls[i] in self.table_values[0]:
                                inside_bets.append(rolls[i])
                                for j in range(len(self.table_values[0])):
                                    if self.table_values[0][j] == rolls[i]:
                                        self.table_values[0][j] = "*" * len(self.table_values[0][j])
                            else:
                                return False
                    self.bets.append(inside_bets)    
            else:
                self.bets.append(rolls[0])
                if rolls[0] == "green":
                    self.table_values[4][0] = "*" * len(self.table_values[4][0])
                    self.table_values[4][1] = "*" * len(self.table_values[4][1])
                    self.table_values[5] = "*" * len(self.table_values[5])#will not be graphically shown, but will signify that green has already been betted on
                else:
                    for i in range(len(self.table_values)):
                        for j in range(len(self.table_values[i])):
                            if rolls[0] == self.table_values[i][j]:
                                self.table_values[i][j] = "*" * len(self.table_values[i][j])
            return True
        else:
            return False
    def verify_bet(self, roll):
        checkInt = roll.split()
        try:
            int(checkInt[0])#checks if the dollar amount is an integer
            checkInt = checkInt[1:]
            check_bet = ' '.join(checkInt)
            rolls = check_bet.split(", ")
            for i in range(len(self.table_values)):
                if rolls[0] in self.table_values[i]:
                    return True
            return False
        except:
            return False
    def calculate_bets(self, roll, bet_money):
        for i in range(len(self.bets)):
            if len(self.bets[i]) > 1:
                return True
    def roulette_rules(self):#prints the rules of roulette
        print("RULES:")
        print("Maximum of 6 bets (Remainder will go back into balance) OR bet until inputted amount is 0 ")
        print("TYPES OF BETS:")
        print("\"INSIDE\" BETS")
        print("Straight-up number bet Syntax: [Dollar amount] [int] (37 to 1)")
        print("Split number bet Syntax: [Dollar amount] [int, int] (17 to 1)")
        print("Street bet Syntax: [Dollar amount] [int, int, int] (11 to 1)")
        print("Corner bet Syntax: [Dollar amount] [int, int, int, int] (8 to 1)")
        print("5-line bet Syntax: [Dollar amount] [int, int, int, int, int] (6 to 1)")
        print("6-line bet Syntax: [Dollar amount] [int, int, int, int, int, int] (5 to 1)")
        print("\"OUTSIDE\" BETS")
        print("Color betting syntax: [Dollar amount] [Red OR Black OR Green] (1 to 1 OR 1 35 to 1 for Green)")
        print("Even or odd betting syntax: [Dollar amount] [Even OR Odd] (1 to 1)")
        print("Column betting syntax: [Dollar amount] [1st row OR 2nd row OR 3rd row] (2 to 1)")
        print("Dozen betting syntax: [Dollar amount] [1st 12 OR 2nd 12 OR 3rd 12] (2 to 1)")
        print("High-or-Low bet syntax: [Dollar amount] [1-18 OR 19-36] (1 to 1)")
    def create_table_values(self):
        total_table = []
        total_table.append([str(i) for i in range(0, 37)])#inside bets
        total_table.append(["1st 12", "2nd 12", "3rd 12"])#outside bet row 1
        total_table.append(["1-18", "even", "red", "black", "odd", "19-36"]) #outside bet row 2
        total_table.append(["1st row", "2nd row", "3rd row"]) #outside bet row 3
        total_table.append(["0", "00"])
        total_table.append("green")
        return total_table

class roulette_table:
    def __init__(self, table_values, bets):#prints an ascii visualization of a roulette table, will cover whatever spot the player bets on
        #first row
        for i in range(74):
            sys.stdout.write("_")
        sys.stdout.write("\n< "+ table_values[4][1] +" | ")
        for i in table_values[0][3::3]:
            sys.stdout.write(i + " | " )
        sys.stdout.write(" " + table_values[3][2] +" |\n")
        self.print_table_lines()
        #second row
        sys.stdout.write("|    | ")
        for i in table_values[0][2::3]:
            sys.stdout.write(i + " | ")
        sys.stdout.write(" " + table_values[3][1] +" |\n")
        self.print_table_lines()
        #third row
        sys.stdout.write("< " + table_values[4][0] + "  | ")
        for i in table_values[0][1::3]:
            sys.stdout.write(i + " | ")
        sys.stdout.write(" " + table_values[3][0] +" |\n")
        for i in range(74):
            if i in [5, 22, 42, 62]:
                sys.stdout.write("|")
            else:
                sys.stdout.write("‾")
        #fourth row
        sys.stdout.write("\n     |")
        for i in table_values[1]:
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
                    sys.stdout.write(" " + table_values[2][i] + "  |")
                case 1:
                    sys.stdout.write("  " + table_values[2][i].upper() + "  |")
                case 2:
                    sys.stdout.write("   " + table_values[2][i].upper() + "   |")
                case 3:
                    sys.stdout.write("  " + table_values[2][i].upper() + "  |")
                case 4:
                    sys.stdout.write("   " + table_values[2][i].upper() + "   |")
                case 5:
                    sys.stdout.write("  " + table_values[2][i] + "  |")
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
    
        
        



