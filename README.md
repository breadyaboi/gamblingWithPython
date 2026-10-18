# gamblingWithPython
### Gamble. With Python
## 1. Running the Game
### MULTIPLAYER (FOR CSE548)
Running this game is pretty much exactly like running werewolves, but if you don't know what werewolves is or you haven't done the assignment, then here's how to run it.
1. Open the Releases page in this Github repository and download either the tar or the zip file within your Ubuntu or any other VM of choice.
2. Extract the file into a folder.
3. Open a terminal in the extracted folder
4. Run (with sudo) this command (you can choose the number of players you want to create):
   ```sudo bash poker_install.sh [number of players]```
   You will see an output like this:
   <img width="577" height="252" alt="image" src="https://github.com/user-attachments/assets/1a04aa94-f290-482e-984c-89f66ae7dfb5" />
   These will be the names of the dealer and players you will be able to access
6. ssh into the dealer account and a number of corresponding player accounts (player0, player1,...) in each terminal.

   ```sudo su [account name]```
8. Within the dealer account, run dealer.py with ```python3 dealer.py```, which will set up a socket for players to connect to.
9. Within each of the player accounts, run poker_client.py with ```python3 poker_client.py```, which will connect the player to the dealer socket.
10. Enjoy gambling in poker.
### SINGLEPLAYER (FOR MY OWN INTERESTS/POSSIBLY YOUR PLEASURE)
1. Download the GamblePython zip folder.
2. Extract the zip.
3. Open a terminal in the folder.
4. Run ```python3 main.py```
5. Enjoy
## 2. Game rules
### MULTIPLAYER (FOR CSE548)
- This is essentially Texas Hold-em poker, it a very famous card game and so rules for this game can be explained way better elsewhere than what I can do, so here's a website explaining it: https://bicyclecards.com/how-to-play/texas-holdem-poker
UI-Wise
- You are given a base amount of $1000 to bet with and if you lose all of your money then you can no longer play.
- Calling and folding you can just type in **call** and **fold** in any case to use the action.
- The syntax for raising the pot is **raise:[dollar amount]**
- You can chat to other players using **chat:[words]**
