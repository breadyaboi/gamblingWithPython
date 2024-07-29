import random
from cards import Deck
import cards
import sys
deck = Deck()

class Blackjack:
    def calculate_card(self, card):
        if card in ["Jack", "Queen", "King"]:
            return 10
        elif card == "Ace":
            return 11
        else:
            return int(card)  # Convert card to integer
class Poker:
    def translate_rank(self, card):
        match card:
            case "Ace":
                return "A"
            case "King":
                return "K"
            case "Queen":
                return "Q"
            case "Jack":
                return "J"
            case _:
                return card
    def translate_suit(self, card):
        match card:
            case "Spades":
                return "♠"
            case "Hearts":
                return "♥"
            case "Diamonds":
                return "♦"
            case "Clubs":
                return "♦"
    def create_card(self, cards):
        for i in cards:
            sys.stdout.write(" _____  ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("|  " + self.translate_suit(card.suit) + "  | ")
        sys.stdout.write("\n") 
        for card in cards:
            if(card.rank == "10"):
                sys.stdout.write("|  " + self.translate_rank(card.rank) + " | ")
            else:
                sys.stdout.write("|  " + self.translate_rank(card.rank) + "  | ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("|  " + self.translate_suit(card.suit) + "  | ")
        sys.stdout.write("\n") 
        for i in cards:
            sys.stdout.write(" ‾‾‾‾‾  ")
        print("\n")
        #return " ___\n" + "| " + suit_design + " |\n" + "| " + rank_design + " |\n" + "| " + suit_design + " |\n" + " ‾‾‾"
class Roulette:
    reds = [str(s) + " Red" for s in [9, 30, 7, 32, 5, 34, 3, 36, 1, 27, 25, 12, 19, 18, 21, 16, 23, 14]]
    blacks = [str(s) + " Black" for s in [28, 26, 11, 20, 17, 22, 15, 24, 13, 10, 29, 8, 31, 6, 33, 4, 35, 2]]
    probability = 7400
    def __init__(self):
        self.roulette_wheel = random.shuffle(self.blacks + self.reds + ["0 Green"])
    def roll_wheel(self):
        landing = random.randint(0, self.probability) % 37
        print("You landed on", self.roulette_wheel[landing])