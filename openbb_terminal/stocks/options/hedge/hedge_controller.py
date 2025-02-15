""" Hedge Controller Module """
__docformat__ = "numpy"

import argparse
import logging
from datetime import datetime
from typing import Dict, List

import pandas as pd
from prompt_toolkit.completion import NestedCompleter

from openbb_terminal import feature_flags as obbff
from openbb_terminal.decorators import log_start_end
from openbb_terminal.helper_funcs import (
    check_non_negative,
    parse_known_args_and_warn,
    print_rich_table,
)
from openbb_terminal.menu import session
from openbb_terminal.parent_classes import BaseController
from openbb_terminal.rich_config import console
from openbb_terminal.stocks.options.hedge import hedge_view
from openbb_terminal.stocks.options.hedge.hedge_model import add_hedge_option
from openbb_terminal.stocks.options.yfinance_model import (
    get_option_chain,
    get_price,
)
from openbb_terminal.stocks.options.yfinance_view import plot_payoff

# pylint: disable=R0902


logger = logging.getLogger(__name__)


class HedgeController(BaseController):
    """Hedge Controller class"""

    CHOICES_COMMANDS = [
        "list",
        "add",
        "rmv",
        "pick",
        "sop",
        "plot",
    ]

    PATH = "/stocks/options/hedge/"

    def __init__(self, ticker: str, expiration: str, queue: List[str] = None):
        """Constructor"""
        super().__init__(queue)

        self.underlying_asset_position: str = ""
        self.chain = get_option_chain(ticker, expiration)
        self.calls = list(
            zip(
                self.chain.calls["strike"].tolist(),
                self.chain.calls["impliedVolatility"].tolist(),
                self.chain.calls["lastPrice"].tolist(),
            )
        )
        self.puts = list(
            zip(
                self.chain.puts["strike"].tolist(),
                self.chain.puts["impliedVolatility"].tolist(),
                self.chain.puts["lastPrice"].tolist(),
            )
        )

        self.PICK_CHOICES = [
            f"{strike} {position} {side}"
            for strike in range(int(self.calls[0][0]), int(self.calls[-1][0]), 5)
            for position in ["Long", "Short"]
            for side in ["Call", "Put"]
        ]

        self.ticker = ticker
        self.current_price: float = get_price(ticker)
        self.expiration = expiration
        self.implied_volatility = self.chain.calls["impliedVolatility"]
        self.options: Dict = {"Portfolio": {}, "Option A": {}, "Option B": {}}
        self.underlying = 0.0
        self.side: str = ""
        self.amount = 0.0
        self.strike = 0.0
        self.call_index_choices = range(len(self.calls))
        self.put_index_choices = range(len(self.puts))
        self.greeks: Dict = {"Portfolio": {}, "Option A": {}, "Option B": {}}

        if session and obbff.USE_PROMPT_TOOLKIT:
            choices: dict = {c: None for c in self.controller_choices}
            choices["pick"] = {c: None for c in self.PICK_CHOICES}
            choices["add"] = {
                str(c): {} for c in list(range(max(len(self.puts), len(self.calls))))
            }
            # This menu contains dynamic choices that may change during runtime
            self.choices = choices
            self.completer = NestedCompleter.from_nested_dict(choices)

    def update_runtime_choices(self):
        """Update runtime choices"""
        if self.options and session and obbff.USE_PROMPT_TOOLKIT:
            self.choices["rmv"] = {c: None for c in ["Option A", "Option B"]}
            self.completer = NestedCompleter.from_nested_dict(self.choices)

    def print_help(self):
        """Print help"""
        has_portfolio_start = "" if "Delta" in self.greeks["Portfolio"] else "[unvl]"
        has_portfolio_end = "" if "Delta" in self.greeks["Portfolio"] else "[/unvl]"
        has_option_start = (
            ""
            if "Delta" in self.greeks["Option A"] or "Delta" in self.greeks["Option B"]
            else "[unvl]"
        )
        has_option_end = (
            ""
            if "Delta" in self.greeks["Option A"] or "Delta" in self.greeks["Option B"]
            else "[/unvl]"
        )
        help_text = f"""
[param]Ticker: [/param]{self.ticker or None}
[param]Expiry: [/param]{self.expiration or None}
[cmds]
    pick          pick the underlying asset position
[/cmds][param]
Underlying Asset Position: [/param]{self.underlying_asset_position}
[cmds]
    list          show the available strike prices for calls and puts{has_portfolio_start}
    add           add an option to the list of options{has_portfolio_end}{has_option_start}
    rmv           remove an option from the list of options
    sop           show selected options and neutral portfolio weights
    plot          show the option payoff diagram[/cmds]{has_option_end}
        """
        console.print(text=help_text, menu="Stocks - Options - Hedge")

    def custom_reset(self):
        """Class specific component of reset command"""
        if self.ticker:
            if self.expiration:
                return [
                    "stocks",
                    f"load {self.ticker}",
                    "options",
                    f"exp -d {self.expiration}",
                    "hedge",
                ]
            return ["stocks", f"load {self.ticker}", "options", "hedge"]
        return []

    @log_start_end(log=logger)
    def call_list(self, other_args):
        """Lists available calls and puts"""
        parser = argparse.ArgumentParser(
            add_help=False,
            prog="list",
            description="""Lists available calls and puts.""",
        )
        ns_parser = parse_known_args_and_warn(parser, other_args)

        if ns_parser:
            calls = pd.DataFrame([call[0] for call in self.calls])
            puts = pd.DataFrame([put[0] for put in self.puts])

            options = pd.concat([calls, puts], axis=1).fillna("-")

            print_rich_table(
                options,
                title="Available Calls and Puts",
                headers=list(["Calls", "Puts"]),
                show_index=True,
                index_name="Identifier",
            )

            console.print("")

    @log_start_end(log=logger)
    def call_add(self, other_args: List[str]):
        """Process add command"""
        parser = argparse.ArgumentParser(
            add_help=False,
            prog="add",
            description="""Add options to the diagram.""",
        )
        parser.add_argument(
            "-p",
            "--put",
            dest="put",
            action="store_true",
            help="Buy a put instead of a call",
            default=False,
        )
        parser.add_argument(
            "-s",
            "--short",
            dest="short",
            action="store_true",
            help="Short the option instead of buying it",
            default=False,
        )
        parser.add_argument(
            "-i",
            "--identifier",
            dest="identifier",
            type=check_non_negative,
            help="The identifier of the option as found in the list command",
            required="-h" not in other_args and "-k" not in other_args,
            choices=self.put_index_choices
            if "-p" in other_args
            else self.call_index_choices,
        )
        if other_args and "-" not in other_args[0][0]:
            other_args.insert(0, "-i")
        ns_parser = parse_known_args_and_warn(parser, other_args)

        if ns_parser:
            if not self.greeks["Portfolio"]:
                console.print(
                    "Please set the Underlying Asset Position by using the 'pick' command.\n"
                )
            else:
                opt_type = "Put" if ns_parser.put else "Call"
                sign = -1 if ns_parser.short else 1
                options_list = self.puts if ns_parser.put else self.calls

                if ns_parser.identifier < len(options_list):
                    strike = options_list[ns_parser.identifier][0]
                    implied_volatility = options_list[ns_parser.identifier][1]
                    cost = options_list[ns_parser.identifier][2]

                    option = {
                        "type": opt_type,
                        "sign": sign,
                        "strike": strike,
                        "implied_volatility": implied_volatility,
                        "cost": cost,
                    }

                    print(cost)

                    if opt_type == "Call":
                        side = 1
                    else:
                        # Implies the option type is a put
                        side = -1

                    date_obj = datetime.strptime(self.expiration, "%Y-%m-%d")
                    days = float((date_obj - datetime.now()).days + 1)

                    if days == 0.0:
                        days = 0.01

                    if "Delta" not in self.greeks["Option A"]:
                        self.options["Option A"] = option
                        (
                            self.greeks["Option A"]["Delta"],
                            self.greeks["Option A"]["Gamma"],
                            self.greeks["Option A"]["Vega"],
                        ) = hedge_view.add_and_show_greeks(
                            self.current_price,
                            implied_volatility,
                            strike,
                            days / 365,
                            side,
                        )
                    elif "Delta" not in self.greeks["Option B"]:
                        self.options["Option B"] = option
                        (
                            self.greeks["Option B"]["Delta"],
                            self.greeks["Option B"]["Gamma"],
                            self.greeks["Option B"]["Vega"],
                        ) = hedge_view.add_and_show_greeks(
                            self.current_price,
                            implied_volatility,
                            strike,
                            days / 365,
                            side,
                        )
                    else:
                        console.print(
                            "[red]The functionality only accepts two options. Therefore, please remove an "
                            "option with 'rmv' before continuing.[/red]\n"
                        )
                        return

                    positions = pd.DataFrame()

                    for _, values in self.options.items():
                        # Loops over options in the dictionary. If a position is empty, skips the printing.
                        if values:
                            option_position: str = (
                                "Long" if values["sign"] == 1 else "Short"
                            )
                            positions = positions.append(
                                [
                                    [
                                        values["type"],
                                        option_position,
                                        values["strike"],
                                        values["implied_volatility"],
                                    ]
                                ]
                            )

                    positions.columns = ["Type", "Hold", "Strike", "Implied Volatility"]

                    print_rich_table(
                        positions,
                        title="Current Option Positions",
                        headers=list(positions.columns),
                        show_index=False,
                    )

                    if (
                        "Delta" in self.greeks["Option A"]
                        and "Delta" in self.greeks["Option B"]
                    ):
                        hedge_view.show_calculated_hedge(
                            self.amount, option["type"], self.greeks, sign
                        )

                    self.update_runtime_choices()
                    console.print("")
        else:
            console.print("Please use a valid index\n")

    @log_start_end(log=logger)
    def call_rmv(self, other_args: List[str]):
        """Process rmv command"""
        parser = argparse.ArgumentParser(
            add_help=False,
            prog="rmv",
            description="""Remove one of the options to be shown in the hedge.""",
        )
        parser.add_argument(
            "-o",
            "--option",
            dest="option",
            help="index of the option to remove",
            nargs="+",
        )
        parser.add_argument(
            "-a",
            "--all",
            dest="all",
            action="store_true",
            help="remove all of the options",
            default=False,
        )
        if other_args and "-" not in other_args[0][0]:
            other_args.insert(0, "-o")
        ns_parser = parse_known_args_and_warn(parser, other_args)
        if ns_parser:
            if not self.options["Option A"] and not self.options["Option B"]:
                console.print("Please add Options by using the 'add' command.\n")
            else:
                if ns_parser.all:
                    self.options = {"Option A": {}, "Option B": {}}
                else:
                    option_name = " ".join(ns_parser.option)

                    if option_name in self.options:
                        self.options[option_name] = {}
                        self.greeks[option_name] = {}

                        self.update_runtime_choices()
                    else:
                        console.print(f"{option_name} is not an option.")

                if self.options["Option A"] or self.options["Option B"]:
                    positions = pd.DataFrame()

                    for _, value in self.options.items():
                        if value:
                            option_side: str = "Long" if value["sign"] == 1 else "Short"
                            positions = positions.append(
                                [
                                    [
                                        value["type"],
                                        option_side,
                                        value["strike"],
                                        value["implied_volatility"],
                                    ]
                                ]
                            )

                    positions.columns = ["Type", "Hold", "Strike", "Implied Volatility"]

                    print_rich_table(
                        positions,
                        title="Current Option Positions",
                        headers=list(positions.columns),
                        show_index=False,
                    )

                console.print("")
        else:
            console.print(
                "No options have been selected, removing them is not possible\n"
            )

    @log_start_end(log=logger)
    def call_pick(self, other_args: List[str]):
        """Process pick command"""
        parser = argparse.ArgumentParser(
            add_help=False,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            prog="long",
            description="This function plots option hedge diagrams",
        )
        parser.add_argument(
            "-p",
            "--pick",
            dest="pick",
            nargs="+",
            help="Choose what you would like to pick",
            required="-h" not in other_args,
        )

        parser.add_argument(
            "-a",
            "--amount",
            dest="amount",
            default=1000,
            help="Choose the amount invested",
        )

        if other_args and "-" not in other_args[0][0]:
            other_args.insert(0, "-p")
        ns_parser = parse_known_args_and_warn(parser, other_args)
        if ns_parser:
            strike_type, underlying_type, side_type = ns_parser.pick
            amount_type = ns_parser.amount

            self.underlying_asset_position = (
                f"{underlying_type} {side_type} {amount_type} @ {strike_type}"
            )

            if underlying_type == "Short":
                self.underlying = -1
            else:
                self.underlying = 1

            if side_type == "Put":
                self.side = "Put"
                side = -1
            else:
                self.side = "Call"
                side = 1

            self.amount = float(amount_type)
            self.strike = strike_type

            index = -1
            date_obj = datetime.strptime(self.expiration, "%Y-%m-%d")
            days = float((date_obj - datetime.now()).days + 1)

            if days == 0.0:
                days = 0.01

            if side == -1:
                for i in range(len(self.chain.puts["strike"])):
                    if self.chain.puts["strike"][i] == self.strike:
                        index = i
                        break
                implied_volatility = self.chain.puts["impliedVolatility"].iloc[index]
            else:
                for i in range(len(self.chain.calls["strike"])):
                    if self.chain.calls["strike"][i] == self.strike:
                        index = i
                        break
                implied_volatility = self.chain.calls["impliedVolatility"].iloc[index]

            (
                self.greeks["Portfolio"]["Delta"],
                self.greeks["Portfolio"]["Gamma"],
                self.greeks["Portfolio"]["Vega"],
            ) = add_hedge_option(
                self.current_price,
                implied_volatility,
                float(self.strike),
                days / 365,
                side,
            )

    @log_start_end(log=logger)
    def call_sop(self, other_args):
        """Process sop command"""
        parser = argparse.ArgumentParser(
            add_help=False,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            prog="sop",
            description="Displays selected option",
        )
        ns_parser = parse_known_args_and_warn(parser, other_args)
        if ns_parser:
            if not self.options["Option A"] and not self.options["Option B"]:
                console.print("Please add Options by using the 'add' command.\n")
            else:
                positions = pd.DataFrame()

                for _, value in self.options.items():
                    if value:
                        option_side: str = "Long" if value["sign"] == 1 else "Short"
                        positions = positions.append(
                            [
                                [
                                    value["type"],
                                    option_side,
                                    value["strike"],
                                    value["implied_volatility"],
                                ]
                            ]
                        )

                positions.columns = ["Type", "Hold", "Strike", "Implied Volatility"]

                print_rich_table(
                    positions,
                    title="Current Option Positions",
                    headers=list(positions.columns),
                    show_index=False,
                )

                if (
                    "Delta" in self.greeks["Option A"]
                    and "Delta" in self.greeks["Option B"]
                ):
                    hedge_view.show_calculated_hedge(
                        self.amount,
                        self.options["Option A"]["type"],
                        self.greeks,
                        self.options["Option A"]["sign"],
                    )

                console.print("")

    @log_start_end(log=logger)
    def call_plot(self, other_args):
        """Process plot command"""
        parser = argparse.ArgumentParser(
            add_help=False,
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
            prog="plot",
            description="This function plots option payoff diagrams",
        )
        ns_parser = parse_known_args_and_warn(parser, other_args)
        if ns_parser:
            plot_payoff(
                self.current_price,
                [self.options["Option A"], self.options["Option B"]],
                self.underlying,
                self.ticker,
                self.expiration,
            )
