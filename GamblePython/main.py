import random
import math
from gambling import *
from banking import BankAccount
from cards import Deck
def play_game(play_again):
        print(play_again)
        play = input('> ').lower()
        if play == "no":
            print("See you next time!")
            exit(0)
#main
game = True
while game == True:
    deck = Deck()#create deck of cards
    balance = BankAccount(1000)
    print("Which gambling game do you want to play? (Blackjack/Poker/Roulette)")
    card_game = input('> ').lower()
    if card_game == "blackjack":
        blackJ = Blackjack()
        while balance.is_neg() == False:
            print("Your current balance is $", balance.show_balance())
            comp = 0
            player = 0
            move = "hit"
            money = -1
            deck.shuffle()
            while money <= 0 or money > balance.show_balance():
                print("How much money do you want to bet? (Integer value)")
                amount = input('> ')
                money = int(amount)
                if(money <= 0 or money > balance.show_balance()):
                    print("Please input a valid amount of money.")
            while comp < 21 and player < 21:
                if move == "hit" or move == "double":
                    playerDraw = deck.draw_card()
                    print("You drew", playerDraw)
                    player = player + blackJ.calculate_card(playerDraw.rank)
                    if player <= 21:
                        compDraw = deck.draw_card()
                        print("Dealer draws", compDraw)
                        comp = comp + blackJ.calculate_card(compDraw.rank)
                elif move == "stand":
                    compDraw = deck.draw_card()
                    print("Dealer draws", compDraw)
                    comp = comp + blackJ.calculate_card(compDraw.rank)
                    if comp > player and comp < 21:
                        print("Dealer's hand:", comp)
                        break
                print("Dealer's hand:", comp)
                print("Your hand: ", player)
                if (comp < 21 and player < 21) and move != "stand":
                    print("What do you want to do next? (Double/Hit/Stand/Surrender)")
                    move = input('> ').lower()
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
            play_game("Do you wish to play another game?: (Yes/No)")
        play_game("You have run out of money, do you wish to restart? (Yes / No)")
    elif card_game == "poker":
        poker = Poker()
        deck.shuffle()
        player_hand = []
        player_hand.append(deck.draw_card())
        player_hand.append(deck.draw_card())
        print("Your hand:")
        poker.create_card(player_hand)
        comp_hand = []
        comp_hand.append(deck.draw_card())
        comp_hand.append(deck.draw_card())
        deck.reset_deck()
    elif card_game == "roulette":
        roulette = Roulette()
        print("Your current balance is", balance.show_balance())
        print("How much money do you want to bet?")
        gamble = input('> ')
        while gamble > 0:
            print("What do you want to bet on? ([Dollar Amount] ******Black/Red/Green/Odds/Evens) Gamble amount left:", gamble)
            roll = input('> ')
        
        roulette.roll_wheel()
    
