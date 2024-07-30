from gambling import *
#main
game = True
while game == True:
    print("Which gambling game do you want to play? (Blackjack/Poker(Texas Hold'em)/Roulette)")
    card_game = input('> ').lower()
    if card_game == "blackjack":
        blackJ = Blackjack()
    elif card_game == "poker":
        poker = Poker()
    elif card_game == "roulette":
        roulette = Roulette()
    
