import random
import sys
class Card:
    def __init__(self, suit, rank):
        self.suit = suit #defines and returns the card's suit
        self.rank = rank #defines and returns the card's rank

    def __str__(self):
        return f'{self.rank} of {self.suit}' #returns the name of the card
    
#Defines a deck of cards
class Deck:
    suits = ['Hearts', 'Diamonds', 'Clubs', 'Spades']
    ranks = ['2', '3', '4', '5', '6', '7', '8', '9', '10', 'Jack', 'Queen', 'King', 'Ace']

    def __init__(self):
        self.cards = [Card(suit, rank) for suit in self.suits for rank in self.ranks]#creates a deck of cards by making as many unique combinations of suits and ranks (4x13 = 52 cards)

    def shuffle(self):#shuffles the deck of cards
        random.shuffle(self.cards)

    def draw_card(self):#draws a card from the deck and removes that card from the deck to avoid duplication
        return self.cards.pop() if self.cards else None

    def reset_deck(self):#puts back all of the cards into the deck in unshuffled order
        self.cards = [Card(suit, rank) for suit in self.suits for rank in self.ranks]

    def calculate_card(self, card):
        if card in ["Jack", "Queen", "King"]:
            return 10
        elif card == "Ace":
            return 11
        else:
            return int(card)  # Convert card to integer
    def translate_rank(self, rank): # Reads the rank and returns the first letter
        return rank[0]
    def translate_suit(self, suit): # Reads the suit of the card and returns the corresponding symbol
        match suit:
            case "Spades":
                return "♠"
            case "Hearts":
                return "♥"
            case "Diamonds":
                return "♦"
            case "Clubs":
                return "♦"
    def create_concealed_hand(self, cards):
        for i in cards:
            sys.stdout.write(" _____  ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("| PY  | ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("| TH  | ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("| ON  | ")
        sys.stdout.write("\n") 
        for i in cards:
            sys.stdout.write(" ‾‾‾‾‾  ")
        sys.stdout.write("\n") 
    def create_hand(self, cards): # Creates an ASCII version of the player's hand 
        for i in cards:
            sys.stdout.write(" _____  ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("|  " + self.translate_suit(card.suit) + "  | ")
        sys.stdout.write("\n") 
        for card in cards:
            if(card.rank == "10"): #prevents offset jank
                sys.stdout.write("|  " + self.translate_rank(card.rank) + " | ")
            else:
                sys.stdout.write("|  " + self.translate_rank(card.rank) + "  | ")
        sys.stdout.write("\n") 
        for card in cards:
            sys.stdout.write("|  " + self.translate_suit(card.suit) + "  | ")
        sys.stdout.write("\n") 
        for i in cards:
            sys.stdout.write(" ‾‾‾‾‾  ")
        sys.stdout.write("\n") 

    def __str__(self):#returns how many cards are left in the deck
        return f'Deck of {len(self.cards)} cards'
    