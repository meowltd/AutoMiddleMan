import json
import discord
from discord.ext import commands
import requests
import blockcypher
from pycoingecko import CoinGeckoAPI
import asyncio
import csv

# Load config from config.json
with open('config.json') as f:
    config = json.load(f)

with open('settings.json') as f:
    settings = json.load(f)

# Initialize CoinGecko API
cg = CoinGeckoAPI()

# Define the Blockcypher API key, Discord user ID, and bot token from config
api_key = config["api_key"]
your_discord_user_id = config["your_discord_user_id"]
bot_token = config["bot_token"]

# Initialize Discord bot
bot = commands.Bot(command_prefix="/", intents=discord.Intents.all())

# Dictionary to track ticket counts for users
user_tickets = {}

def get_ltc_to_usd_price():
    response = cg.get_price(ids='litecoin', vs_currencies='usd')
    return response['litecoin']['usd']

def usd_to_satoshis(usd_amount):
    ltc_to_usd_price = get_ltc_to_usd_price()
    ltc_price_in_satoshis = 100_000_000
    satoshis_amount = int(usd_amount / ltc_to_usd_price * ltc_price_in_satoshis)
    return satoshis_amount

def create_new_ltc_address():
    endpoint = f"https://api.blockcypher.com/v1/ltc/main/addrs?token={api_key}"
    response = requests.post(endpoint)
    
    if response.status_code != 201:
        print(f"Error: Received unexpected status code {response.status_code}")
        print(f"Response content: {response.content}")
        return None, None
    
    data = response.json()
    if "address" not in data or "private" not in data:
        print("Error: Missing 'address' or 'private' key in response data")
        print(f"Response data: {data}")
        return None, None
    
    new_address = data["address"]
    private_key = data["private"]
    with open('keylogs.txt', 'a') as f:
        f.write(f"{new_address} | {private_key}\n")
    return new_address, private_key

def get_address_balance(address):
    endpoint = f"https://api.blockcypher.com/v1/ltc/main/addrs/{address}/balance?token={api_key}"
    response = requests.get(endpoint)
    data = response.json()
    balance = data.get("balance", 0)
    unconfirmed_balance = data.get("unconfirmed_balance", 0)
    return balance, unconfirmed_balance

def send_ltc(private_key, recipient_address, amount):
    try:
        fee_estimate = 100_000  # 0.001 LTC
        amount_after_fee = max(0, amount - fee_estimate)
        
        if amount_after_fee <= 0:
            raise Exception("Amount after fee is too low to send")

        tx = blockcypher.simple_spend(
            from_privkey=private_key,
            to_address=recipient_address,
            to_satoshis=amount_after_fee,
            api_key=api_key,
            coin_symbol="ltc"
        )
        return tx
    except Exception as e:
        print(f"Error sending LTC: {e}")
        return None

def generate_qr_code(address, amount):
    qr_code_url = f"https://api.qrserver.com/v1/create-qr-code/?data=litecoin:{address}?amount={amount:.8f}&size=200x200"
    return qr_code_url

def read_product():
    with open('accounts.csv', 'r') as file:
        reader = csv.reader(file)
        products = list(reader)
    if products:
        return products[0][0], products[1:]  # First product and remaining products
    return None, []

def write_products(products):
    with open('accounts.csv', 'w', newline='') as file:
        writer = csv.writer(file)
        writer.writerows(products)

def get_stock():
    with open('accounts.csv', 'r') as file:
        reader = csv.reader(file)
        products = list(reader)
    return len(products)

def increment_user_ticket_count(user_id):
    if user_id not in user_tickets:
        user_tickets[user_id] = 0
    user_tickets[user_id] += 1

def decrement_user_ticket_count(user_id):
    if user_id in user_tickets:
        user_tickets[user_id] = max(0, user_tickets[user_id] - 1)

class CopyButtons(discord.ui.View):
    def __init__(self, address, amount):
        super().__init__(timeout=None)
        self.address = address
        self.amount = amount

        self.add_item(discord.ui.Button(label="Copy Address", style=discord.ButtonStyle.primary, custom_id="copy_address"))
        self.add_item(discord.ui.Button(label="Copy Amount", style=discord.ButtonStyle.primary, custom_id="copy_amount"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "copy_address":
            await interaction.response.send_message(self.address, ephemeral=True)
        elif interaction.data["custom_id"] == "copy_amount":
            await interaction.response.send_message(f"{self.amount:.8f}", ephemeral=True)
        return True

class PurchaseDropdown(discord.ui.Select):
    def __init__(self, price, product):
        options = [
            discord.SelectOption(label="Manual Purchase", description="Manual purchase process"),
            discord.SelectOption(label="Auto Purchase [LTC]", description="Automatic Litecoin purchase"),
        ]
        super().__init__(placeholder="Select Purchase Method", min_values=1, max_values=1, options=options)
        self.price = price
        self.product = product

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Manual Purchase":
            await manual_purchase(interaction, self.price, self.product)
        elif self.values[0] == "Auto Purchase [LTC]":
            await auto_purchase(interaction, self.price, self.product)

class PurchaseView(discord.ui.View):
    def __init__(self, price, product):
        super().__init__(timeout=None)
        self.add_item(PurchaseDropdown(price, product))

async def manual_purchase(interaction: discord.Interaction, price, product):
    guild = interaction.guild
    user = interaction.user
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
    }
    category = discord.utils.get(guild.categories, name="Manual Purchases")
    if category is None:
        category = await guild.create_category("Manual Purchases")
    channel = await guild.create_text_channel(f"manual-purchase-{user.name}", overwrites=overwrites, category=category)
    embed = discord.Embed(description="This is a private channel for your manual purchase. Click the button below to close this channel when you are done.", color=0x57beff)
    view = CloseChannelButton(channel)
    await channel.send(embed=embed, view=view)
    await interaction.response.send_message(f"A private channel has been created for your manual purchase: {channel.mention}", ephemeral=True)

async def auto_purchase(interaction: discord.Interaction, price, product):
    await interaction.response.send_message("Check your DMs for further instructions.", ephemeral=True)
    user = interaction.user

    # Create DM channel
    dm_channel = await user.create_dm()

    # Calculate the required amount in satoshis and LTC
    required_usd = price
    required_satoshis = usd_to_satoshis(required_usd)
    required_ltc = required_satoshis / 100_000_000
    
    # Step 1: Make Litecoin Address
    new_address, private_key = create_new_ltc_address()

    if not new_address or not private_key:
        await dm_channel.send(
            embed=discord.Embed(
                description="Failed to create a new Litecoin address. Please try again later.",
                color=0xff6b6b
            )
        )
        return

    # Generate QR code URL
    qr_code_url = generate_qr_code(new_address, required_ltc)

    # Step 2: Send payment instructions in DM
    embed = discord.Embed(
        description=(
            f"Please send {required_ltc:.8f} LTC (approximately ${required_usd}) to the following address:\n"
            f"```\n{new_address}\n```"
        ),
        color=0x57beff
    )
    embed.set_image(url=qr_code_url)
    view = CopyButtons(new_address, required_ltc)
    msg = await dm_channel.send(embed=embed, view=view)

    # Wait for the payment
    while True:
        balance, unconfirmed_balance = get_address_balance(new_address)
        if unconfirmed_balance >= required_satoshis:
            await msg.delete()
            await dm_channel.send(
                embed=discord.Embed(
                    description="<a:Loading:1259059860486623315> Payment **received**, awaiting confirmations.",
                    color=0x7cff6b
                )
            )
            break
        await asyncio.sleep(30)

    # Step 4: Confirm the payment
    while True:
        balance, unconfirmed_balance = get_address_balance(new_address)
        if balance >= required_satoshis:
            await dm_channel.send(
                embed=discord.Embed(
                    description="<:Litecoin:1259060274904830003> Payment **confirmed**. Retrieving your product.",
                    color=0x57beff
                )
            )
            break
        await asyncio.sleep(30)

    # Step 5: Send the received money to the LTC address specified in settings.json
    tx = send_ltc(private_key, settings["LTC_Address"], balance)
    if tx:
        product, remaining_products = read_product()
        if product:
            write_products(remaining_products)
            await dm_channel.send(
                embed=discord.Embed(
                    description=f"<:bank:1259060836908007526> Here is your product: `{product}` | Thanks for using us.",
                    color=0x7cff6b
                )
            )
        else:
            await dm_channel.send(
                embed=discord.Embed(
                    description="No products available. Please contact support.",
                    color=0xff6b6b
                )
            )
    else:
        await dm_channel.send(
            embed=discord.Embed(
                description="Failed to send LTC. Please check the transaction details and try again.",
                color=0xff6b6b
            )
        )

    # Auto close the ticket
    category = discord.utils.get(interaction.guild.categories, name="Auto MM")
    if category and interaction.channel.category == category:
        await interaction.channel.delete()
        decrement_user_ticket_count(interaction.user.id)

class AccountForm(discord.ui.Modal, title="Account Panel"):
    price = discord.ui.TextInput(label="Price", placeholder="50", required=True)
    product = discord.ui.TextInput(label="Product", placeholder="email:reccode", required=True)

    async def on_submit(self, interaction: discord.Interaction):
        price = float(self.price.value)
        product = self.product.value
        embed = discord.Embed(title="Account", description="Buy This Account", color=0x57beff)
        view = PurchaseView(price, product)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message("Panel created successfully.", ephemeral=True)

@bot.tree.command(name="accountpanel", description="Create an account panel for selling")
async def accountpanel(interaction: discord.Interaction):
    if interaction.user.id != int(your_discord_user_id):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    await interaction.response.send_modal(AccountForm())

@bot.tree.command(name="buy", description="Purchase an item using Litecoin")
async def buy(interaction: discord.Interaction):
    await interaction.response.send_message("Check your DMs for further instructions.", ephemeral=True)
    user = interaction.user

    # Create DM channel
    dm_channel = await user.create_dm()

    # Read required USD amount from settings
    required_usd = settings["Required_USD_Amount"]
    
    # Step 1: Make Litecoin Address
    new_address, private_key = create_new_ltc_address()

    if not new_address or not private_key:
        await dm_channel.send(
            embed=discord.Embed(
                description="Failed to create a new Litecoin address. Please try again later.",
                color=0xff6b6b
            )
        )
        return
    
    # Calculate the required amount in satoshis and LTC
    required_satoshis = usd_to_satoshis(required_usd)
    required_ltc = required_satoshis / 100_000_000
    
    # Generate QR code URL
    qr_code_url = generate_qr_code(new_address, required_ltc)

    # Step 2: Send payment instructions in DM
    embed = discord.Embed(
        description=(
            f"Please send {required_ltc:.8f} LTC (approximately ${required_usd}) to the following address:\n"
            f"```\n{new_address}\n```"
        ),
        color=0x57beff
    )
    embed.set_image(url=qr_code_url)
    view = CopyButtons(new_address, required_ltc)
    msg = await dm_channel.send(embed=embed, view=view)

    # Wait for the payment
    while True:
        balance, unconfirmed_balance = get_address_balance(new_address)
        if unconfirmed_balance >= required_satoshis:
            await msg.delete()
            await dm_channel.send(
                embed=discord.Embed(
                    description="<a:Loading:1259059860486623315> Payment **received**, awaiting confirmations.",
                    color=0x7cff6b
                )
            )
            break
        await asyncio.sleep(30)

    # Step 4: Confirm the payment
    while True:
        balance, unconfirmed_balance = get_address_balance(new_address)
        if balance >= required_satoshis:
            await dm_channel.send(
                embed=discord.Embed(
                    description="<:Litecoin:1259060274904830003> Payment **confirmed**. Retrieving your product.",
                    color=0x57beff
                )
            )
            break
        await asyncio.sleep(30)

    # Step 5: Send the received money to the LTC address specified in settings.json
    tx = send_ltc(private_key, settings["LTC_Address"], balance)
    if tx:
        product, remaining_products = read_product()
        if product:
            write_products(remaining_products)
            await dm_channel.send(
                embed=discord.Embed(
                    description=f"<:bank:1259060836908007526> Here is your product: `{product}` | Thanks for using us.",
                    color=0x7cff6b
                )
            )
        else:
            await dm_channel.send(
                embed=discord.Embed(
                    description="No products available. Please contact support.",
                    color=0xff6b6b
                )
            )
    else:
        await dm_channel.send(
            embed=discord.Embed(
                description="Failed to send LTC. Please check the transaction details and try again.",
                color=0xff6b6b
            )
        )

    # Auto close the ticket
    category = discord.utils.get(interaction.guild.categories, name="Auto MM")
    if category and interaction.channel.category == category:
        await interaction.channel.delete()
        decrement_user_ticket_count(interaction.user.id)

@bot.tree.command(name="stock", description="Check the current stock of products")
async def stock(interaction: discord.Interaction):
    stock_count = get_stock()
    await interaction.response.send_message(f"Stock: {stock_count}", ephemeral=True)

@bot.tree.command(name="close", description="Close the current ticket channel (Admin only)")
async def close(interaction: discord.Interaction):
    if interaction.user.id != int(your_discord_user_id):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return
    
    channel = interaction.channel
    if channel.category and channel.category.name in ["Auto MM", "Manual Purchases"]:
        await channel.delete()
        decrement_user_ticket_count(interaction.user.id)
        await interaction.response.send_message(f"Channel {channel.name} has been closed.", ephemeral=True)
    else:
        await interaction.response.send_message("Channel is not a ticket.", ephemeral=True)

class CloseChannelButton(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel
        self.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.danger, custom_id="close_channel"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "close_channel":
            await self.channel.delete()
            decrement_user_ticket_count(interaction.user.id)
            await interaction.response.send_message("Channel closed.", ephemeral=True)
        return True

class RoleSelectionView(discord.ui.View):
    def __init__(self, channel, user, other_user):
        super().__init__(timeout=None)
        self.channel = channel
        self.user = user
        self.other_user = other_user
        self.sender = None
        self.receiver = None
        self.role_msg = None
        self.add_item(discord.ui.Button(label="Sender", style=discord.ButtonStyle.primary, custom_id="set_sender"))
        self.add_item(discord.ui.Button(label="Receiver", style=discord.ButtonStyle.primary, custom_id="set_receiver"))
        self.add_item(discord.ui.Button(label="Reset", style=discord.ButtonStyle.secondary, custom_id="reset_roles"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "set_sender":
            await self.set_sender(interaction)
        elif interaction.data["custom_id"] == "set_receiver":
            await self.set_receiver(interaction)
        elif interaction.data["custom_id"] == "reset_roles":
            await self.reset_roles(interaction)
        return True

    async def set_sender(self, interaction: discord.Interaction):
        if interaction.user not in [self.user, self.other_user]:
            await interaction.response.send_message("You are not part of this transaction.", ephemeral=True)
            return
        if self.receiver == interaction.user:
            await interaction.response.send_message("You cannot be both the sender and the receiver.", ephemeral=True)
            return
        if self.sender is not None:
            await interaction.response.send_message("Role is already taken.", ephemeral=True)
            return
        self.sender = interaction.user
        await self.update_roles()
        await interaction.response.send_message("Successfully selected your role as sender.", ephemeral=True)

    async def set_receiver(self, interaction: discord.Interaction):
        if interaction.user not in [self.user, self.other_user]:
            await interaction.response.send_message("You are not part of this transaction.", ephemeral=True)
            return
        if self.sender == interaction.user:
            await interaction.response.send_message("You cannot be both the sender and the receiver.", ephemeral=True)
            return
        if self.receiver is not None:
            await interaction.response.send_message("Role is already taken.", ephemeral=True)
            return
        self.receiver = interaction.user
        await self.update_roles()
        await interaction.response.send_message("Successfully selected your role as receiver.", ephemeral=True)

    async def reset_roles(self, interaction: discord.Interaction):
        self.sender = None
        self.receiver = None
        await self.update_roles()
        await interaction.response.send_message("Roles have been reset.", ephemeral=True)

    async def update_roles(self):
        embed = discord.Embed(description=f"# User Confirmation\n\nPlease note, that once the roles have been confirmed by both users, you cannot go back. You will have to start a new deal if the roles are incorrect.\n\n**Sender: **{self.sender.mention if self.sender else 'None'}\n**Reciever: **{self.receiver.mention if self.receiver else 'None'}")
        if self.role_msg:
            await self.role_msg.edit(embed=embed)
        else:
            self.role_msg = await self.channel.send(embed=embed)
        if self.sender and self.receiver:
            await self.role_msg.delete()  # Delete role selection message
            view = RoleConfirmationView(self.channel, self.sender, self.receiver)
            await self.channel.purge()  # Delete all previous messages in the channel
            self.role_msg = await self.channel.send(view=view)

class RoleConfirmationView(discord.ui.View):
    def __init__(self, channel, sender, receiver):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.confirmations = {sender: False, receiver: False}
        self.process_started = False  # Flag to prevent multiple processes
        self.add_item(discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success, custom_id="confirm_roles"))
        self.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel_roles"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_roles":
            await self.confirm_roles(interaction)
        elif interaction.data["custom_id"] == "cancel_roles":
            await self.cancel_roles(interaction)
        return True

    async def confirm_roles(self, interaction: discord.Interaction):
        if interaction.user not in [self.sender, self.receiver]:
            await interaction.response.send_message("You are not part of this transaction.", ephemeral=True)
            return

        self.confirmations[interaction.user] = True

        if all(self.confirmations.values()):
            await interaction.response.send_message("Roles confirmed successfully.", ephemeral=True)
            if not self.process_started:
                self.process_started = True  # Mark process as started
                await self.channel.purge()  # Delete all previous messages in the channel
                await self.ask_for_amount()
            await interaction.message.delete()
        else:
            await interaction.response.send_message("Successfully Confirmed", ephemeral=True)

    async def cancel_roles(self, interaction: discord.Interaction):
        await interaction.response.send_message("Roles have been reset. Please select your roles again.", ephemeral=True)
        role_selection_embed = discord.Embed(description="Please select your role:", color=0x57beff)
        view = RoleSelectionView(self.channel, self.sender, self.receiver)
        await self.channel.send(embed=role_selection_embed, view=view)
        await interaction.message.delete()

    async def ask_for_amount(self):
        amount_message = await self.channel.send(embed=discord.Embed(description="Please enter the amount in USD (Minimum $1).", color=0x57beff))

        def check(m):
            return m.channel == self.channel and m.author == self.sender

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                amount = float(msg.content)
                await msg.delete()
                if amount >= 1:
                    await self.confirm_amount(amount)
                    await amount_message.delete()
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Amount must be at least $1. Please enter a valid amount.", color=0xff6b6b))
            except ValueError:
                await self.channel.send(embed=discord.Embed(description="Invalid amount. Please enter a valid number.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return

    async def confirm_amount(self, amount):
        embed = discord.Embed(description=f"Amount: ${amount}\nConfirm the amount.", color=0x57beff)
        view = AmountConfirmationView(self.channel, self.sender, self.receiver, amount)
        amount_msg = await self.channel.send(embed=embed, view=view)
        view.amount_msg = amount_msg  # Link the amount confirmation message to the view

class AmountConfirmationView(discord.ui.View):
    def __init__(self, channel, sender, receiver, amount):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.amount = amount
        self.amount_msg = None
        self.confirmations = {sender: False, receiver: False}
        self.add_item(discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success, custom_id="confirm_amount"))
        self.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger, custom_id="cancel_amount"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_amount":
            await self.confirm_amount(interaction)
        elif interaction.data["custom_id"] == "cancel_amount":
            await self.cancel_amount(interaction)
        return True

    async def confirm_amount(self, interaction: discord.Interaction):
        if interaction.user not in [self.sender, self.receiver]:
            await interaction.response.send_message("You are not part of this transaction.", ephemeral=True)
            return

        self.confirmations[interaction.user] = True

        if all(self.confirmations.values()):
            await interaction.response.send_message("Successfully Confirmed", ephemeral=True)
            await self.create_ltc_address()
            await self.amount_msg.delete()
            await interaction.message.delete()
        else:
            await interaction.response.send_message("Successfully Confirmed", ephemeral=True)

    async def cancel_amount(self, interaction: discord.Interaction):
        await interaction.response.send_message("Amount entry cancelled. Please enter the amount again.", ephemeral=True)
        await self.ask_for_amount()
        await self.amount_msg.delete()
        await interaction.message.delete()

    async def ask_for_amount(self):
        amount_message = await self.channel.send(embed=discord.Embed(description="Please enter the amount in USD (Minimum $1).", color=0x57beff))

        def check(m):
            return m.channel == self.channel and m.author == self.sender

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                amount = float(msg.content)
                await msg.delete()
                if amount >= 1:
                    await self.confirm_amount(amount)
                    await amount_message.delete()
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Amount must be at least $1. Please enter a valid amount.", color=0xff6b6b))
            except ValueError:
                await self.channel.send(embed=discord.Embed(description="Invalid amount. Please enter a valid number.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return

    async def create_ltc_address(self):
        async for message in self.channel.history():
            if message.author == bot.user:
                await message.delete()

        new_address, private_key = create_new_ltc_address()
        required_satoshis = usd_to_satoshis(self.amount)
        required_ltc = required_satoshis / 100_000_000

        qr_code_url = generate_qr_code(new_address, required_ltc)

        embed = discord.Embed(
            description=(
                f"# <a:Loading:1259779057697034240> Waiting To Receive LTC\n"
                f"Send exactly {required_ltc:.8f} LTC (**${self.amount}**) to the address below:\n"
                f"\n ```{new_address}```\n"
                "\n\n### Once the funds have been received by the bot, you may proceed with the deal."
            ),
            color=0x57beff
        )
        embed.set_image(url=qr_code_url)
        view = CopyButtons(new_address, required_ltc)
        msg = await self.channel.send(embed=embed, view=view)

        await self.wait_for_transaction(new_address, private_key, msg)

    async def wait_for_transaction(self, address, private_key, msg):
        required_satoshis = usd_to_satoshis(self.amount)
        while True:
            balance, unconfirmed_balance = get_address_balance(address)
            if unconfirmed_balance >= required_satoshis:
                await msg.delete()
                await self.channel.send(embed=discord.Embed(description="## <a:Loading:1259779057697034240> Waiting for confirmations.\n\nWe are currently waiting for the transaction to be confirmed. You may proceed with the deal now, as the bot is now in control of the funds.\n\nThe amount shown above is the total amount of LTC the bot has received. The fee will get automatically taken out at the end."))
                break
            await asyncio.sleep(30)

        while True:
            balance, unconfirmed_balance = get_address_balance(address)
            if balance >= required_satoshis:
                await self.channel.send(embed=discord.Embed(description="# <:Litecoin:1259060274904830003> Payment **confirmed**, you may proceed with deal now.", color=0x57beff))
                break
            await asyncio.sleep(30)

        await self.release_funds(private_key, address)

    async def release_funds(self, private_key, address):
        embed = discord.Embed(description="## If you wish to release click the button, there is no turning back after you confirm release.", color=0x57beff)
        view = ReleaseFundsView(self.channel, self.sender, self.receiver, private_key, address)
        release_msg = await self.channel.send(embed=embed, view=view)
        view.release_msg = release_msg  # Link the release confirmation message to the view

class ReleaseFundsView(discord.ui.View):
    def __init__(self, channel, sender, receiver, private_key, address):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.private_key = private_key
        self.address = address
        self.release_msg = None
        self.funds_released = False
        self.add_item(discord.ui.Button(label="Release Funds", style=discord.ButtonStyle.success, custom_id="release_funds"))
        self.add_item(discord.ui.Button(label="Return Funds", style=discord.ButtonStyle.danger, custom_id="return_funds"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "release_funds":
            await self.release_funds(interaction)
        elif interaction.data["custom_id"] == "return_funds":
            await self.return_funds(interaction)
        return True

    async def release_funds(self, interaction: discord.Interaction):
        if interaction.user != self.sender:
            await interaction.response.send_message("Only the sender can release the funds.", ephemeral=True)
            return

        if self.funds_released:
            await interaction.response.send_message("Funds are already released.", ephemeral=True)
            return

        embed = discord.Embed(description="Are you sure you would like to release the funds?", color=0x57beff)
        view = ConfirmReleaseView(self.channel, self.sender, self.receiver, self.private_key, self.address)
        self.release_msg = await self.channel.send(embed=embed, view=view)
        await interaction.message.delete()

    async def return_funds(self, interaction: discord.Interaction):
        if interaction.user != self.receiver:
            await interaction.response.send_message("Only the receiver can return the funds.", ephemeral=True)
            return

        embed = discord.Embed(description="Are you sure you would like to return the funds?", color=0x57beff)
        view = ConfirmReturnView(self.channel, self.sender, self.receiver, self.private_key, self.address)
        self.release_msg = await self.channel.send(embed=embed, view=view)
        await interaction.message.delete()

class ConfirmReleaseView(discord.ui.View):
    def __init__(self, channel, sender, receiver, private_key, address):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.private_key = private_key
        self.address = address
        self.add_item(discord.ui.Button(label="Yes", style=discord.ButtonStyle.success, custom_id="confirm_release"))
        self.add_item(discord.ui.Button(label="No", style=discord.ButtonStyle.danger, custom_id="cancel_release"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_release":
            await self.confirm_release(interaction)
        elif interaction.data["custom_id"] == "cancel_release":
            await self.cancel_release(interaction)
        return True

    async def confirm_release(self, interaction: discord.Interaction):
        if interaction.user != self.sender:
            await interaction.response.send_message("Only the sender can confirm the release.", ephemeral=True)
            return

        await self.channel.send(embed=discord.Embed(description="Receiver, please provide your Litecoin address.", color=0x57beff))

        def check(m):
            return m.channel == self.channel and m.author == self.receiver

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                receiver_ltc_address = msg.content
                if self.is_valid_ltc_address(receiver_ltc_address):
                    embed = discord.Embed(description=f"Are you sure this is your LTC address?\n```\n{receiver_ltc_address}\n```", color=0x57beff)
                    view = ConfirmLTCAddressView(self.channel, self.sender, self.receiver, self.private_key, self.address, receiver_ltc_address)
                    confirm_ltc_msg = await self.channel.send(embed=embed, view=view)
                    view.confirm_ltc_msg = confirm_ltc_msg  # Link the confirmation message to the view
                    await msg.delete()
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Invalid LTC address. Please enter a valid address.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return
        await interaction.message.delete()

    async def cancel_release(self, interaction: discord.Interaction):
        await interaction.response.send_message("Release cancelled.", ephemeral=True)
        await self.release_msg.delete()
        await interaction.message.delete()

    def is_valid_ltc_address(self, address):
        return address.startswith('L') or address.startswith('M')

class ConfirmReturnView(discord.ui.View):
    def __init__(self, channel, sender, receiver, private_key, address):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.private_key = private_key
        self.address = address
        self.add_item(discord.ui.Button(label="Yes", style=discord.ButtonStyle.success, custom_id="confirm_return"))
        self.add_item(discord.ui.Button(label="No", style=discord.ButtonStyle.danger, custom_id="cancel_return"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_return":
            await self.confirm_return(interaction)
        elif interaction.data["custom_id"] == "cancel_return":
            await self.cancel_return(interaction)
        return True

    async def confirm_return(self, interaction: discord.Interaction):
        if interaction.user != self.receiver:
            await interaction.response.send_message("Only the receiver can confirm the return.", ephemeral=True)
            return

        await self.channel.send(embed=discord.Embed(description="Sender, please provide your Litecoin address for the refund.", color=0x57beff))

        def check(m):
            return m.channel == self.channel and m.author == self.sender

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                sender_ltc_address = msg.content
                if self.is_valid_ltc_address(sender_ltc_address):
                    embed = discord.Embed(description=f"Are you sure this is your LTC address?\n```\n{sender_ltc_address}\n```", color=0x57beff)
                    view = ConfirmReturnLTCAddressView(self.channel, self.sender, self.receiver, self.private_key, self.address, sender_ltc_address)
                    confirm_ltc_msg = await self.channel.send(embed=embed, view=view)
                    view.confirm_ltc_msg = confirm_ltc_msg  # Link the confirmation message to the view
                    await msg.delete()
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Invalid LTC address. Please enter a valid address.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return
        await interaction.message.delete()

    async def cancel_return(self, interaction: discord.Interaction):
        await interaction.response.send_message("Return cancelled.", ephemeral=True)
        await self.release_msg.delete()
        await interaction.message.delete()

    def is_valid_ltc_address(self, address):
        return address.startswith('L') or address.startswith('M')

class ConfirmLTCAddressView(discord.ui.View):
    def __init__(self, channel, sender, receiver, private_key, address, receiver_ltc_address):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.private_key = private_key
        self.address = address
        self.receiver_ltc_address = receiver_ltc_address
        self.funds_released = False
        self.add_item(discord.ui.Button(label="Yes", style=discord.ButtonStyle.success, custom_id="confirm_ltc_address"))
        self.add_item(discord.ui.Button(label="No", style=discord.ButtonStyle.danger, custom_id="cancel_ltc_address"))
        self.confirm_ltc_msg = None

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_ltc_address":
            await self.confirm_ltc_address(interaction)
        elif interaction.data["custom_id"] == "cancel_ltc_address":
            await self.cancel_ltc_address(interaction)
        return True

    async def confirm_ltc_address(self, interaction: discord.Interaction):
        if interaction.user != self.receiver:
            await interaction.response.send_message("Only the receiver can confirm the LTC address.", ephemeral=True)
            return

        tx = send_ltc(self.private_key, self.receiver_ltc_address, get_address_balance(self.address)[0])
        if tx:
            self.funds_released = True
            await self.channel.send(embed=discord.Embed(description=f"# Transaction Completed: \n Money Release To: `{self.receiver_ltc_address}.`", color=0x7cff6b))
            # Add Close Ticket button
            view = CloseChannelButton(self.channel)
            await self.channel.send(embed=discord.Embed(description=f"## Money Release To: {self.receiver_ltc_address}. Click the button below to close the ticket.", color=0x57beff), view=view)
        else:
            await self.channel.send(embed=discord.Embed(description="Failed to send LTC. Please check the transaction details and try again.", color=0xff6b6b))
        await self.confirm_ltc_msg.delete()
        await interaction.message.delete()

    async def cancel_ltc_address(self, interaction: discord.Interaction):
        await interaction.response.send_message("Please provide a valid LTC address.", ephemeral=True)
        await self.channel.send(embed=discord.Embed(description="Receiver, please provide your Litecoin address.", color=0x57beff))
        await interaction.message.delete()

        def check(m):
            return m.channel == self.channel and m.author == self.receiver

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                receiver_ltc_address = msg.content
                if self.is_valid_ltc_address(receiver_ltc_address):
                    embed = discord.Embed(description=f"Are you sure this is your LTC address?\n```\n{receiver_ltc_address}\n```", color=0x57beff)
                    view = ConfirmLTCAddressView(self.channel, self.sender, self.receiver, self.private_key, self.address, receiver_ltc_address)
                    confirm_ltc_msg = await self.channel.send(embed=embed, view=view)
                    view.confirm_ltc_msg = confirm_ltc_msg  # Link the confirmation message to the view
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Invalid LTC address. Please enter a valid address.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return

    def is_valid_ltc_address(self, address):
        return address.startswith('L') or address.startswith('M')

class ConfirmReturnLTCAddressView(discord.ui.View):
    def __init__(self, channel, sender, receiver, private_key, address, sender_ltc_address):
        super().__init__(timeout=None)
        self.channel = channel
        self.sender = sender
        self.receiver = receiver
        self.private_key = private_key
        self.address = address
        self.sender_ltc_address = sender_ltc_address
        self.funds_returned = False
        self.add_item(discord.ui.Button(label="Yes", style=discord.ButtonStyle.success, custom_id="confirm_ltc_address"))
        self.add_item(discord.ui.Button(label="No", style=discord.ButtonStyle.danger, custom_id="cancel_ltc_address"))
        self.confirm_ltc_msg = None

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "confirm_ltc_address":
            await self.confirm_ltc_address(interaction)
        elif interaction.data["custom_id"] == "cancel_ltc_address":
            await self.cancel_ltc_address(interaction)
        return True

    async def confirm_ltc_address(self, interaction: discord.Interaction):
        if interaction.user != self.sender:
            await interaction.response.send_message("Only the sender can confirm the LTC address.", ephemeral=True)
            return

        tx = send_ltc(self.private_key, self.sender_ltc_address, get_address_balance(self.address)[0])
        if tx:
            self.funds_returned = True
            await self.channel.send(embed=discord.Embed(description=f"# Transaction Completed: \n Money Returned To: `{self.receiver_ltc_address}.`", color=0x7cff6b))
            # Add Close Ticket button
            view = CloseChannelButton(self.channel)
            await self.channel.send(embed=discord.Embed(description=f"## Money Release To: {self.receiver_ltc_address}. Click the button below to close the ticket.", color=0x57beff), view=view)
        else:
            await self.channel.send(embed=discord.Embed(description="Failed to send LTC. Please check the transaction details and try again.", color=0xff6b6b))
        await self.confirm_ltc_msg.delete()
        await interaction.message.delete()

    async def cancel_ltc_address(self, interaction: discord.Interaction):
        await interaction.response.send_message("Please provide a valid LTC address.", ephemeral=True)
        await self.channel.send(embed=discord.Embed(description="Sender, please provide your Litecoin address for the refund.", color=0x57beff))
        await interaction.message.delete()

        def check(m):
            return m.channel == self.channel and m.author == self.sender

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                sender_ltc_address = msg.content
                if self.is_valid_ltc_address(sender_ltc_address):
                    embed = discord.Embed(description=f"Are you sure this is your LTC address?\n```\n{sender_ltc_address}\n```", color=0x57beff)
                    view = ConfirmReturnLTCAddressView(self.channel, self.sender, self.receiver, self.private_key, self.address, sender_ltc_address)
                    confirm_ltc_msg = await self.channel.send(embed=embed, view=view)
                    view.confirm_ltc_msg = confirm_ltc_msg  # Link the confirmation message to the view
                    break
                else:
                    await self.channel.send(embed=discord.Embed(description="Invalid LTC address. Please enter a valid address.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await self.channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return

    def is_valid_ltc_address(self, address):
        return address.startswith('L') or address.startswith('M')




@bot.tree.command(name="autommpanel", description="Create an auto MM panel for escrow service")
async def autommpanel(interaction: discord.Interaction):
    if interaction.user.id != int(your_discord_user_id):
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    embed = discord.Embed(title="Auto MM Panel", description="Click the button below to start the auto MM process.", color=0x57beff)
    view = StartAutoMMView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Auto MM panel created successfully.", ephemeral=True)

class StartAutoMMView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label="Start", style=discord.ButtonStyle.primary, custom_id="start_auto_mm"))

    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.data["custom_id"] == "start_auto_mm":
            await self.start_auto_mm(interaction)
        return True

    async def start_auto_mm(self, interaction: discord.Interaction):
        guild = interaction.guild
        user = interaction.user

        if user_tickets.get(user.id, 0) >= 2:
            await interaction.response.send_message("You already have the maximum number of tickets open (2).", ephemeral=True)
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        category = discord.utils.get(guild.categories, name="Auto MM")
        if category is None:
            category = await guild.create_category("Auto MM")
        channel = await guild.create_text_channel(f"auto-mm-{user.name}", overwrites=overwrites, category=category)

        increment_user_ticket_count(user.id)

        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
        query_message = await channel.send(embed=discord.Embed(description="### Provide the User ID of the user you are trading with.\n\n**Example:** `123456789012345678`"))

        def check(m):
            return m.channel == channel and m.author == user

        while True:
            try:
                msg = await bot.wait_for('message', check=check, timeout=60.0)
                other_user_id = int(msg.content)
                other_user = guild.get_member(other_user_id)
                if other_user and other_user_id != user.id:
                    overwrites[other_user] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    await channel.edit(overwrites=overwrites)
                    user_added_msg = await channel.send(embed=discord.Embed(description="# User Identification\n\nThe **Sender** will be sending the funds **to** @Pearl Auto.\nThe **Receiver** will be receiving the funds **from** @Pearl Auto."))
                    await msg.delete()
                    await query_message.delete()
                    break
                else:
                    await channel.send(embed=discord.Embed(description="Invalid or duplicate User ID. Please try again.", color=0xff6b6b))
            except ValueError:
                await channel.send(embed=discord.Embed(description="Invalid User ID. Please try again.", color=0xff6b6b))
            except asyncio.TimeoutError:
                await channel.send(embed=discord.Embed(description="Timeout. Please start the process again.", color=0xff6b6b))
                return
        await user_added_msg.delete()
        embed = discord.Embed(description="Please select your role:", color=0x57beff)
        view = RoleSelectionView(channel, user, other_user)
        role_select_msg = await channel.send(embed=embed, view=view)
        view.role_msg = role_select_msg  # Link the role selection message to the view

@bot.event
async def on_ready():
    await bot.tree.sync()
    print("Bot Ready")

bot.run(bot_token)
