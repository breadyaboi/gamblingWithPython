import random
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

    def __str__(self):#returns how many cards are left in the deck
        return f'Deck of {len(self.cards)} cards'