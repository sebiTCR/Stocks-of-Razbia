import math

from API import API

from database import Company
import database
from multi_arg import IntOrStrAll, CompanyOrIntOrAll
import commands as commands_
import time


def register_commands(api: API):
    company = api.group(name="company")
    my = api.group(name="my")
    all = api.group(name='all')
    next = api.group(name='next')

    @all.command()
    async def commands(ctx):
        thing_to_display = []
        for command_name, command in ctx.api.commands.items():
            if isinstance(command, commands_.Command):
                thing_to_display.append(f'!{command_name}')
            else:
                thing_to_display.append(f'!{command_name}[{", ".join(command.sub_commands)}]')

        ctx.api.send_chat_message(f"All minigame-related chat commands: {', '.join(thing_to_display)}")

    @api.command(usage="<company> <amount>")
    async def buy(ctx, company: CompanyOrIntOrAll, amount: CompanyOrIntOrAll):
        if isinstance(company, Company) and not isinstance(amount, Company):
            if amount == 'all':
                amount = company.remaining_shares
        elif not isinstance(company, Company) and isinstance(amount, Company):
            company, amount = amount, company
            if amount == 'all':
                amount = company.remaining_shares
        elif isinstance(company, Company) and isinstance(amount, Company):
            ctx.api.send_chat_message(f"@{ctx.user.name} inputted 2 companies. You need to input a value and a company")
            return
        elif not isinstance(company, Company) and not isinstance(amount, Company):
            ctx.api.send_chat_message(f"@{ctx.user.name} didn't input any company. You need to input a value and a company")
            return

        points = await ctx.user.points(ctx.api)
        cost = math.ceil(amount*company.stock_price)

        if company.remaining_shares == 0:
            ctx.api.send_chat_message(f"@{ctx.user.name} no stocks left to buy at {company.abbv}")
            return
        if amount > company.remaining_shares:
            ctx.api.send_chat_message(f"@{ctx.user.name} tried buying {amount} stocks, but only {company.remaining_shares} are remaining")
            return
        if points >= cost:
            ctx.api.send_chat_message(f"@{ctx.user.name} just bought {amount} {company.abbv} for {cost} {ctx.api.overlord.currency_name}")
            share = ctx.session.query(database.Shares).filter_by(user_id=ctx.user.id, company_id=company.id).first()
            if share:
                share.amount += amount
            else:
                ctx.session.add(database.Shares(user_id=ctx.user.id, company_id=company.id, amount=amount))
            company.increase_chance += .15 * amount
            ctx.session.commit()
            await ctx.api.upgraded_add_points(ctx.user, -cost, ctx.session)
        else:
            ctx.api.send_chat_message(f"@{ctx.user.name} has {points} {ctx.api.overlord.currency_name} and requires {cost} aka {cost-points} more")

    @api.command(usage="<company> <amount>")
    async def sell(ctx, company: CompanyOrIntOrAll, amount: CompanyOrIntOrAll):
        if isinstance(company, Company) and not isinstance(amount, Company):
            pass
            # if amount == 'all':
            #     amount = company.remaining_shares
        elif not isinstance(company, Company) and isinstance(amount, Company):
            company, amount = amount, company
            # if amount == 'all':
            #     amount = company.remaining_shares
        elif isinstance(company, Company) and isinstance(amount, Company):
            ctx.api.send_chat_message(f"@{ctx.user.name} You input 2 companies. you need to input a value and a company")
            return
        elif not isinstance(company, Company) and not isinstance(amount, Company):
            ctx.api.send_chat_message(f"@{ctx.user.name} You didn't input any company. you need to input a value and a company")
            return

        share = ctx.session.query(database.Shares).get((ctx.user.id, company.id))
        if amount == 'all' and share or share and share.amount >= amount:
            if amount == 'all':
                amount = share.amount
            cost = math.ceil(amount * company.stock_price)

            await ctx.api.upgraded_add_points(ctx.user, cost, ctx.session)
            share.amount -= amount
            if share.amount == 0:
                ctx.session.delete(share)
            company.increase_chance -= .15 * amount
            ctx.session.commit()
            ctx.api.send_chat_message(f"@{ctx.user.name} has sold {amount} stock{'s' if share.amount > 1 else ''} for {cost} {ctx.api.overlord.currency_name}")
        else:
            if not share:
                message = f"@{ctx.user.name} doesn't have any stocks at company {company.abbv}."
            else:
                message = f"@{ctx.user.name} doesn't have {amount} stocks at company {company.abbv} to sell them. "\
                          f"They have only {share.amount} stock{'s.' if share.amount > 1 else '.'}"
            ctx.api.send_chat_message(message)

    @company.command(usage="<company>")
    async def info(ctx, company: Company):
        ctx.api.send_chat_message(company)

    @api.command()
    async def companies(ctx):
        res = []
        companies = ctx.session.query(database.Company).order_by(Company.stock_price.desc()).all()
        for company in companies:
            # message = f"{company.abbv.upper()}[{company.stock_price-company.price_diff:.1f}{company.price_diff:+}]"
            # message = f"{company.abbv.upper()}[{company.stock_price:.1f}{company.price_diff/company.stock_price*100:+.1f}%]"
            message = company.announcement_description
            res.append(message)
        ctx.api.send_chat_message(", ".join(res))

    @company.command(usage="<company>")
    async def shares(ctx, company: Company):
        ctx.api.send_chat_message(f"@{ctx.user.name} '{company.abbv}' has {company.remaining_shares} remaining shares")

    @api.command()
    async def stocks(ctx):
        ctx.api.send_chat_message(f"@{ctx.user.name} Minigame basic commands: !buy, !companies, !my shares, !help_minigame, !all commands")

    @api.command()
    async def help_minigame(ctx):
        ctx.api.send_chat_message("This is a stock simulation minigame made by Razbi and Nesami | "
                                  "Company[current_price, price_change] | "
                                  "Price changes each 30 min | "
                                  "!next_month to see when's the price changes | "
                                  "For a more in-depth explanation, please go to "
                                  "https://github.com/TheRealRazbi/Stocks-of-Razbia/blob/master/explanation.txt")

    @api.command()
    async def next_month(ctx):
        time_since_last_run = time.time() - ctx.api.overlord.last_check
        time_till_next_run = ctx.api.overlord.iterate_cooldown - time_since_last_run
        ctx.api.send_chat_message(f"@{ctx.user.name} The next month starts in {time_till_next_run/60:.0f} minutes")

    @my.command()
    async def shares(ctx):
        res = []
        shares = ctx.session.query(database.Shares).filter_by(user_id=ctx.user.id).all()
        if shares:
            for share in shares:
                company = ctx.session.query(Company).get(share.company_id).announcement_description
                res.append(f"{company}: {share.amount}")

            ctx.api.send_chat_message(f'@{ctx.user.name} has {", ".join(res)}')
        else:
            ctx.api.send_chat_message(f"@{ctx.user.name} doesn't own any shares.")

    @my.command()
    async def points(ctx):
        ctx.api.send_chat_message(f"@{ctx.user.name} currently has {await ctx.user.points(ctx.api)} {ctx.api.overlord.currency_name}")

    @my.command()
    async def profit(ctx):
        profit_str = ctx.user.profit_str
        profit = f"@{ctx.user.name} Profit: {profit_str[0]} {ctx.api.overlord.currency_name} | Profit Percentage: {profit_str[1]}"
        ctx.api.send_chat_message(profit)

    @next.command()
    async def month(ctx):
        time_since_last_run = time.time() - ctx.api.overlord.last_check
        time_till_next_run = ctx.api.overlord.iterate_cooldown - time_since_last_run
        ctx.api.send_chat_message(f"@{ctx.user.name} The next month starts in {time_till_next_run/60:.0f} minutes")

    # @api.command()
    # def test_turtle(ctx, thing: IntOrStrAll):
    #     # message = ""
    #     # for i in range(100):
    #     #     message += "thing"
    #
    #     ctx.api.send_chat_message(thing)
    #     ctx.api.send_chat_message(type(thing))


if __name__ == '__main__':
    pass
    # loop = asyncio.get_event_loop()
    # loop.run_until_complete(buy_stocks)
    # b = BuyCommand()
    # print(b.run.__annotations__)
    # buy_stocks(1, "reee")
    # api = API()
    # buy_stocks(1, '1')
    # ctx = Contdext(api, user)
    # prepared_args = prepare_args(ctx, command, args)
    # command(ctx, *args)

