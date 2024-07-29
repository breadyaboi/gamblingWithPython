class BankAccount:
    def __init__(self, balance):
        self.balance = balance
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