import ast
import json
import subprocess
import time
import random

import sqlalchemy as sql

from API import API
import asyncio
from bot_commands import register_commands
from database import User, Company
import database
from termcolor import colored
import math
import os
import config_server
import config_server.forms
from company_names import load_default_names
import webbrowser
from more_tools import CachedProperty
import alembic.config
from customizable_stuff import load_message_templates, load_announcements
from announcements import Announcement, AnnouncementDict


class Overlord:
    def __init__(self, loop=None):
        alembic_extra_args = []
        if os.path.exists('lib/code'):
            alembic_extra_args.extend(('-c', 'lib/code/alembic.ini', '-n', 'deploy'))
        if not os.path.exists('lib/db.sqlite'):
            database.Base.metadata.create_all()
            alembic.config.main(argv=alembic_extra_args+['stamp', 'head'])
        elif not database.engine.dialect.has_table(database.engine, 'alembic_version'):
            # database.Base.metadata.create_all()
            alembic.config.main(argv=alembic_extra_args+['stamp', '0e0024b069d6'])
        alembic.config.main(argv=alembic_extra_args+['upgrade', 'head'])

        session = database.Session()
        self.last_check = 0
        self.loop = loop
        if loop is None:
            self.loop = asyncio.get_event_loop()
        self.api = API(overlord=self, loop=loop)
        register_commands(self.api)
        self.iterate_cooldown = 10*60
        # self.iterate_cooldown = 3
        self.new_companies_messages = ['ughhh... buy from them or smth?', "stonks", "nice, I guess...", "oh yeah, I like that one",
                                       "wait, what are these?", "I like turtles", "who's Razbi?", "Glory to Arstotzka, wait..., wrong game", "buy buy buy"]
        self.bankrupt_companies_messages = ['Feelsbadman', 'just invest better Kappa', 'imagine losing {currency_name} to those Kappa', 'this is fine monkaS']
        self.max_companies = 7
        self.max_companies_at_a_time = 2
        self.spawn_ranges = {
            "poor_range": (7, 15),
            "expensive_range": (15, 35),
        }
        self.max_stocks_owned = 100_000
        self._cache = {}

        self.stock_increase = []
        self.bankrupt_info = []
        self.owners_of_bankrupt_companies = set()

        self.names = {}
        self.load_names()

        self.messages = {}
        self.load_messages()

        self.announcements = {}
        self.load_announcements()

        self.months = 0
        self.load_age(session=session)
        self.started = False
        self.company_events_counter = 0
        session.close()

    async def run(self):
        time_since_last_run = time.time() - self.last_check
        if self.started and time_since_last_run > self.iterate_cooldown:
            self.last_check = time.time()
            session = database.Session()

            await self.iterate_companies(session)
            self.spawn_companies(session)
            await self.clear_bankrupt(session)

            self.display_update()
            self.months += 1
            self.update_age(session)
            session.close()
        else:
            await asyncio.sleep(3)
            # time.sleep(self.iterate_cooldown-time_since_last_run)

    def spawn_companies(self, session: database.Session):
        spawned_companies = []
        companies_count = session.query(Company).count()
        companies_to_spawn = min(self.max_companies - companies_count, self.max_companies_at_a_time)

        if companies_count == 0:
            print(colored("hint: you can type the commands only in your twitch chat", "green"))
            self.api.send_chat_message(f"Welcome to Stocks of Razbia. "
                                       "Naming Convention: company[10.5+9.5%] Number on the left is current price. Number on the right is the price change from last month. "
                                       "tip: you don't have to type company names in caps. ")

        for _ in range(companies_to_spawn):
            random_abbv = random.choice(list(self.names.items()))
            self.names.pop(random_abbv[0])

            if random.random() > 0.6:
                starting_price = round(random.uniform(*self.spawn_ranges["poor_range"]), 3)
            else:
                starting_price = round(random.uniform(*self.spawn_ranges["expensive_range"]), 3)

            company = Company.create(starting_price, name=random_abbv)
            session.add(company)
            spawned_companies.append(f"[{company.abbv}] {company.full_name}, stock_price: {company.stock_price:.1f} {self.currency_name}")
        if spawned_companies:
            tip = random.choice(self.new_companies_messages)
            self.api.send_chat_message(f"Newly spawned companies: {' | '.join(spawned_companies)}, {tip}")

        session.commit()

    async def iterate_companies(self, session: database.Session):
        for index_company, company in enumerate(session.query(Company).all()):
            res = company.iterate()
            if res:
                self.stock_increase.append(True)
                shares = session.query(database.Shares).filter_by(company_id=company.id)
                for share in shares:
                    user = session.query(User).get(share.user_id)
                    cost = user.passive_income(company=company, session=session)
                    # cost = math.ceil(share.amount*company.stock_price)
                    # user = session.query(User).get(share.user_id)
                    # income_percent = 0.10
                    # total_shares = sum(share.amount for share in user.shares if share.company_id == company.id)*company.stock_price
                    # income_percent -= (total_shares/5_000)/100
                    # cost = math.ceil(cost * max(income_percent, 0.01))
                    await self.api.upgraded_add_points(user, cost, session)

                session.commit()
            else:
                if not company.abbv == 'DFLT':
                    company.bankrupt = True
                    self.names[company.abbv] = company.full_name
                    session.commit()
        self.start_company_events(session)

    async def clear_bankrupt(self, session: database.Session):
        for index_company, company in enumerate(session.query(Company).filter_by(bankrupt=True)):
            self.bankrupt_info.append(f'{company.abbv.upper()}')
            shares = session.query(database.Shares).filter_by(company_id=company.id).all()
            for share in shares:
                user = session.query(database.User).get(share.user_id)
                await self.api.upgraded_add_points(user, share.amount, session)
                self.owners_of_bankrupt_companies.add(f'@{user.name}')
                # print(f"Refunded {share.amount} points to @{user.name}")
            session.delete(company)
            session.commit()

    def load_names(self, session: database.Session = None):
        if session is None:
            session = database.Session()
        companies = session.query(Company).all()
        company_names_db = session.query(database.Settings).get('company_names')
        if company_names_db:
            self.names = {item["abbv"]: item["company_name"] for item in json.loads(company_names_db.value)}
        else:
            self.names = {item["abbv"]: item["company_name"] for item in load_default_names()}
            session.add(database.Settings(key='company_names', value=json.dumps(load_default_names())))
            session.commit()
        for company in companies:
            self.names.pop(company.abbv, None)

        session.commit()

    def load_messages(self, session: database.Session = None):
        if session is None:
            session = database.Session()
        messages = session.query(database.Settings).get('messages')
        if messages:
            self.messages = json.loads(messages.value)
            if 'about' in self.messages:
                del self.messages['about']
            default_messages = load_message_templates()
            for default_key in default_messages:
                if default_key not in self.messages:
                    self.messages[default_key] = default_messages[default_key]
                    session = database.Session()
                    session.query(database.Settings).get('messages').value = json.dumps(self.messages)
                    session.commit()
        else:
            # messages = database.Settings(key='messages', value=json.dumps(load_message_templates()))
            # session.add(messages)
            # session.commit()
            self.messages = load_message_templates()
        # print(self.api.commands)

    def load_age(self, session: database.Session):
        age = session.query(database.Settings).get('age')
        if age is None:
            if os.path.exists('lib/age'):
                with open('lib/age', "r") as f:
                    age = f.read()
                session.add(database.Settings(key='age', value=age))
                session.commit()
                os.remove('lib/age')
            else:
                session.add(database.Settings(key='age', value='0'))
                session.commit()
                age = 0
        else:
            age = age.value

        self.months = int(age)

    def update_age(self, session: database.Session):
        age = session.query(database.Settings).get('age')
        age.value = str(self.months)
        session.commit()

    def display_update(self):
        if self.stock_increase:
            # companies_to_announce, owners = self.get_companies_for_updates(session)
            # self.api.send_chat_message(f'Month: {self.months} | {", ".join(self.stock_increase)}')

            # self.api.send_chat_message(f'Year: {int(self.months/12)} | Month: {self.months%12} | '
            #                            f'{", ".join(companies_to_announce)} {" ".join(owners)} | '
            #                            f'Use "!stocks" to display all available commands.')

            # self.api.send_chat_message(f"Minigame basic commands: !introduction, !companies, !my shares, !buy, !all commands, !stocks")

            self.stock_increase = []
        # self.api.send_chat_message(f"Companies: {session.query(Company).count()}/{self.max_companies} Rich: {self.rich}"
        #                            f", Poor: {self.poor}, Most Expensive Company: {self.most_expensive_company(session)}")
        if self.bankrupt_info:
            random_comment = random.choice(self.bankrupt_companies_messages).format(currency_name=self.currency_name)
            self.api.send_chat_message(f'The following companies bankrupt: {", ".join(self.bankrupt_info)} '
                                       f'{" ".join(self.owners_of_bankrupt_companies)} {random_comment if self.owners_of_bankrupt_companies else ""}')
            self.bankrupt_info = []

    async def start_periodic_announcement(self):
        while True:
            if self.started:
                formatter = AnnouncementDict.from_list(self.announcements['element_list'])
                result = Announcement(self.announcements['result'])
                final_message = str(result).format_map(formatter).replace('[currency_name]', self.currency_name)
                # print(final_message)

                # stonks_or_stocks = random.choice(['stocks', 'stonks'])
                # final_message = random.choice([f'Wanna make some {self.currency_name} through stonks?', 'Be the master of stonks.'])+' '
                #
                # main_variation = random.randint(1, 2)
                # if main_variation == 1:
                #     command_order = ['!introduction', '!companies', '!buy', f'!{stonks_or_stocks}', '!autoinvest', '!my income']
                #     random.shuffle(command_order)
                #     help_tip = random.choice(['Here are some commands to help you do that', "Ughh maybe through these?",
                #                               "I wonder what are these for", "Commands", "Turtles"])
                #     final_message += f"{help_tip}: {', '.join(command_order)}"
                # elif main_variation == 2:
                #     final_message += "For newcomers we got: '!autoinvest <budget>'"
                self.api.send_chat_message(final_message)

                await asyncio.sleep(60 * 30)
            else:
                await asyncio.sleep(.5)

    def start_company_events(self, session: database.Session):
        if self.company_events_counter >= 4:
            self.company_events_counter = 0
            companies = session.query(database.Company).filter_by(bankrupt=False).all()
            company_candidates = []
            for company in companies:
                if company.event_months_remaining is None:
                    company.event_months_remaining = 0
                if company.stock_price > 10000 and company.event_months_remaining <= 0:
                    company.event_increase = random.randint(-25, -10)
                    company.event_months_remaining = 3
                if len(company_candidates) < 2 and company.stock_price < 1000:
                    company_candidates.append(company)
                else:
                    for index, company_candidate in enumerate(company_candidates):
                        if company.stocks_bought < company_candidate.stocks_bought and company.stock_price < 1000 and company.event_months_remaining <= 0:
                            company_candidates[index] = company
                            break

            if company_candidates:
                company = random.choice(company_candidates)
                company.event_increase = random.randint(5, 20)
                company.event_months_remaining = random.randint(2, 3)
                session.commit()
                # tip = random.choice([f"{self.messages['stocks_alias']} price it's likely to increase", "Buy Buy Buy", "Time to invest"])
                # self.api.send_chat_message(f"{company.full_name} just released a new product. {tip}.")
                if 'company_released_product' not in self.messages:
                    self.messages['company_released_product'] = load_message_templates()['company_released_product']
                    session = database.Session()
                    session.query(database.Settings).get('messages').value = json.dumps(self.messages)
                    session.commit()
                self.api.send_chat_message(self.messages['company_released_product'].format(currency_name=self.currency_name,
                                                                                            company_full_name=company.full_name,
                                                                                            company_summary=company.announcement_description))

        else:
            self.company_events_counter += 1

    @staticmethod
    def get_companies_for_updates(session: database.Session):
        res = []
        owners = session.query(database.User).filter(database.User.shares).all()
        owners = [f'@{user.name}' for user in owners]

        for company in session.query(Company).filter(Company.shares).all():
            if len(res) >= 5:
                break
            if company.price_diff == 0:
                continue
            # message = f"{company.abbv.upper()}[{company.stock_price:.1f}{company.price_diff/company.stock_price*100:+.1f}%]"
            message = company.announcement_description
            res.append(message)

        if len(res) < 3:
            richest_companies = session.query(database.Company).order_by(Company.stock_price.desc()).limit(3-len(res)).all()
            for company in richest_companies:
                if company.price_diff == 0:
                    continue
                # message = f"{company.abbv.upper()}[{company.stock_price - company.price_diff:.1f}{company.price_diff:+}]"
                # message = f"{company.abbv.upper()}[{company.stock_price:.1f}{company.price_diff/company.stock_price*100:+.1f}%]"
                message = company.announcement_description
                if company.stock_price < 0:
                    print("A company has like stock_price under 0, I have absolutely no idea how was this possible, please tell Razbi about it")
                if message not in res:
                    res.append(message)
        return res, owners

    @CachedProperty
    def currency_name(self):
        session = database.Session()
        if currency_name := session.query(database.Settings).get('currency_name'):
            currency_name = currency_name.value
        else:
            currency_name = 'points'
            session.add(database.Settings(key='currency_name', value=currency_name))
            session.commit()
        return currency_name

    def mark_dirty(self, setting):
        if setting in self._cache:
            del self._cache[setting]

    @staticmethod
    def display_credits():
        print(f"""
--------------------------------------------------------------------------
Welcome to {colored('Stocks of Razbia', 'green')}
This program is open-source and you can read the code at {colored('https://github.com/TheRealRazbi/Stocks-of-Razbia', 'yellow')}
In fact you can even read the code by opening {colored('lib/code/', 'green')}
ALL the minigame files are stored in the same folder as the executable, in the {colored('lib/', 'green')}
You can check for updates when you start the program and pick the other option.
This program was made by {colored('Razbi', 'magenta')} and {colored('Nesami', 'magenta')}
Program started at {colored(str(time.strftime('%H : %M')), 'cyan')}
--------------------------------------------------------------------------\n
        """)

    def load_announcements(self):
        session = database.Session()
        if announcements := session.query(database.Settings).get('announcements'):
            announcements = ast.literal_eval(announcements.value)
        else:
            announcements = load_announcements()
            # session.add(database.Settings(key='announcements', value=repr(announcements)))
            # session.commit()
        self.announcements = announcements


def start(func, overlord: Overlord):
    return overlord.loop.run_until_complete(func)


async def iterate_forever(overlord: Overlord):
    while True:
        await overlord.run()


async def iterate_forever_and_start_reading_chat(overlord: Overlord):
    asyncio.create_task(overlord.api.start_read_chat())
    await iterate_forever(overlord)
    o.api.send_chat_message("")
    await asyncio.sleep(60 * 60 * 365 * 100)


async def iterate_forever_read_chat_and_run_interface(overlord: Overlord):
    if os.path.exists('lib/code'):
        webbrowser.open('http://localhost:5000')
    config_server.app.overlord = overlord
    await asyncio.gather(
        config_server.app.run_task(use_reloader=False),
        iterate_forever(overlord),
        overlord.api.start_read_chat(),
        overlord.api.twitch_key_auto_refresher(),
        overlord.start_periodic_announcement(),
        asyncio.sleep(60 * 60 * 365 * 100),
    )

    await asyncio.sleep(60 * 60 * 365 * 100)


if __name__ == '__main__':
    o = Overlord()
    start(iterate_forever_read_chat_and_run_interface(o), o)
    # webbrowser.open('http://localhost:5000')










