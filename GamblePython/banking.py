class BankAccount:
    initial_amount = 0
    def __init__(self, balance):
        self.balance = balance
        self.initial_amount = balance
    def show_balance(self):
        return self.balance
    def add_balance(self, amount):
        self.balance = self.balance + amount
    def sub_balance(self, amount):
        self.balance = self.balance - amount
    def is_neg(self):#checks if there is any money left in the bank account
        if self.balance <= 0:
            return True
        return False
    def reset_balance(self):
        self.balance = self.initial_amount
    def betting_amount(self, statement):
        amount = 0
        while amount <= 0:
            print(statement)
            try:
                amount = int(input('> '))
                if(amount <= 0):
                    print("Please input a valid amount of money.")
                else:
                    return amount
            except:
                print("Please input a valid amount of money.")