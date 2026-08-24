"""The hand-over: what a warm-up carries into a session, and what it must not.

The first version of this module vetoed every order decided on history. It kept the account
clean and broke the strategy — a setup marks its armed order placed when it *emits* the signal,
so it crossed into the session believing an order rested at a venue that had never heard of it.
Measured on real EURUSD H1 bars with the CHoCH setup, four of five hand-over points produced
that ghost.

So the tests here are written against the thing that went wrong: a **real setup**, over **real
market shapes**, checked for agreement between what the strategy believes and what the broker
holds. A scripted strategy cannot fail that way — it has no bookkeeping to disagree with — and
would have passed the broken design.
"""

from decimal import Decimal

import pytest

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import NoCostModel
from tradeforge_engine.domain import (
    AccountState,
    Candle,
    Context,
    InstrumentSpec,
    OrderRequest,
    OrderResult,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.indicators import EMA, SMA
from tradeforge_engine.loop import iter_run
from tradeforge_engine.protocols import Broker
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.testing import (
    EURUSD,
    HOUR,
    START,
    ImmediateFillBroker,
    ScriptedStrategy,
    bar,
    rising,
)
from tradeforge_engine.warmup import HandOver, hand_over, unwarmed_indicators

CAPITAL = Decimal(10_000)


def a_broker() -> BacktestBroker:
    return BacktestBroker(instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel())


RISK = PercentRiskManager(percent=Decimal(1))


def carry(warm: Broker, live: Broker, *, bars: int = 175) -> HandOver:
    """`hand_over` with the two seams a re-size needs, so a test names only what it varies.

    Typed on `Broker`, not `BacktestBroker`: one scenario deliberately hands it a broker that
    cannot enumerate its resting orders, and narrowing here would make that unwritable.
    """
    return hand_over(warm, live, symbol="EURUSD", bars=bars, risk=RISK, instrument=EURUSD)


def a_context(candle: Candle) -> Context:
    return Context(
        candle=candle,
        instrument=EURUSD,
        account=AccountState(balance=CAPITAL, equity=CAPITAL),
        position=None,
        fills=(),
    )


def warm(candles: list[Candle], strategy: object) -> BacktestBroker:
    """Run history through the real loop against a real broker — a backtest, deliberately."""
    broker = a_broker()
    for _ in iter_run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=strategy,  # type: ignore[arg-type]
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    ):
        pass
    return broker


# One hundred and seventy-five real EURUSD H1 bars, starting 2025-01-01 21:00 UTC.
#
# ⚠️ Real data, and the window was **measured, not chosen**. The criterion matters and it took
# two tries. The first version asked for "one resting order, no trade, flat" — and got a warm-up
# that filled nothing and left the account at exactly its initial capital, which made two of the
# tests below vacuous: `the money does not cross` cannot separate 10 000 from 10 000, and
# `warm-up really does trade` was satisfied by the resting order the window was selected to
# guarantee.
#
# The criterion here is the one the design actually needs: **at least one fill**, at least one
# order still resting, flat at the end, and an account that **moved**. Measured on this window:
# 2 fills, 1 closed trade, 1 resting order, equity 10 000 -> 9 901. That last number is what
# makes the re-sizing observable — the carried order was sized at 1.08 lots against 9 901, and
# the session's own 10 000 calls for 1.09.
#
# The package's synthetic scenarios cannot do any of this: probed, `BULLISH_START`,
# `BULLISH_START_EMA3` and `GAPPING_IMPULSE` arm nothing at all, being far too short for the
# structure machine to confirm a break and mark a block.
#
# Timestamps are `bar()`'s hourly grid rather than the original instants. Only the shape matters:
# the setup reads highs, lows and closes, never the calendar.
ARMS_A_RESTING_LIMIT: tuple[Candle, ...] = (
    bar(0, open_="1.03515", close="1.03556", high="1.03579", low="1.03493"),
    bar(1, open_="1.03548", close="1.03508", high="1.03566", low="1.03464"),
    bar(2, open_="1.03508", close="1.03521", high="1.03565", low="1.03473"),
    bar(3, open_="1.03522", close="1.03579", high="1.03608", low="1.03443"),
    bar(4, open_="1.03579", close="1.03709", high="1.03712", low="1.03576"),
    bar(5, open_="1.03710", close="1.03715", high="1.03742", low="1.03681"),
    bar(6, open_="1.03716", close="1.03693", high="1.03750", low="1.03664"),
    bar(7, open_="1.03692", close="1.03637", high="1.03707", low="1.03618"),
    bar(8, open_="1.03639", close="1.03637", high="1.03649", low="1.03600"),
    bar(9, open_="1.03637", close="1.03628", high="1.03724", low="1.03623"),
    bar(10, open_="1.03628", close="1.03628", high="1.03709", low="1.03463"),
    bar(11, open_="1.03629", close="1.03517", high="1.03706", low="1.03495"),
    bar(12, open_="1.03516", close="1.03167", high="1.03570", low="1.03137"),
    bar(13, open_="1.03167", close="1.03322", high="1.03343", low="1.03150"),
    bar(14, open_="1.03322", close="1.03183", high="1.03343", low="1.03149"),
    bar(15, open_="1.03182", close="1.03134", high="1.03297", low="1.03114"),
    bar(16, open_="1.03135", close="1.03060", high="1.03239", low="1.03022"),
    bar(17, open_="1.03037", close="1.02742", high="1.03089", low="1.02725"),
    bar(18, open_="1.02741", close="1.02613", high="1.02763", low="1.02243"),
    bar(19, open_="1.02610", close="1.02649", high="1.02680", low="1.02514"),
    bar(20, open_="1.02652", close="1.02564", high="1.02662", low="1.02558"),
    bar(21, open_="1.02565", close="1.02513", high="1.02595", low="1.02480"),
    bar(22, open_="1.02511", close="1.02615", high="1.02645", low="1.02506"),
    bar(23, open_="1.02614", close="1.02565", high="1.02675", low="1.02556"),
    bar(24, open_="1.02569", close="1.02642", high="1.02661", low="1.02534"),
    bar(25, open_="1.02652", close="1.02678", high="1.02681", low="1.02644"),
    bar(26, open_="1.02679", close="1.02730", high="1.02735", low="1.02658"),
    bar(27, open_="1.02731", close="1.02676", high="1.02731", low="1.02664"),
    bar(28, open_="1.02675", close="1.02722", high="1.02747", low="1.02667"),
    bar(29, open_="1.02723", close="1.02708", high="1.02745", low="1.02687"),
    bar(30, open_="1.02706", close="1.02697", high="1.02710", low="1.02671"),
    bar(31, open_="1.02697", close="1.02717", high="1.02736", low="1.02646"),
    bar(32, open_="1.02717", close="1.02793", high="1.02794", low="1.02715"),
    bar(33, open_="1.02789", close="1.02808", high="1.02847", low="1.02687"),
    bar(34, open_="1.02808", close="1.02804", high="1.02881", low="1.02728"),
    bar(35, open_="1.02804", close="1.02903", high="1.02999", low="1.02803"),
    bar(36, open_="1.02903", close="1.02814", high="1.02939", low="1.02792"),
    bar(37, open_="1.02813", close="1.02973", high="1.03029", low="1.02813"),
    bar(38, open_="1.02972", close="1.02956", high="1.03010", low="1.02942"),
    bar(39, open_="1.02958", close="1.02932", high="1.03002", low="1.02914"),
    bar(40, open_="1.02931", close="1.03004", high="1.03024", low="1.02879"),
    bar(41, open_="1.02947", close="1.02892", high="1.03058", low="1.02731"),
    bar(42, open_="1.02891", close="1.02909", high="1.02999", low="1.02835"),
    bar(43, open_="1.02910", close="1.02980", high="1.03057", low="1.02909"),
    bar(44, open_="1.02979", close="1.02973", high="1.03021", low="1.02924"),
    bar(45, open_="1.02969", close="1.03012", high="1.03061", low="1.02938"),
    bar(46, open_="1.03011", close="1.03057", high="1.03075", low="1.02988"),
    bar(47, open_="1.03056", close="1.03067", high="1.03099", low="1.03046"),
    bar(48, open_="1.03018", close="1.03007", high="1.03060", low="1.02954"),
    bar(49, open_="1.03010", close="1.03049", high="1.03079", low="1.03009"),
    bar(50, open_="1.03051", close="1.03030", high="1.03053", low="1.02953"),
    bar(51, open_="1.03029", close="1.03121", high="1.03136", low="1.03012"),
    bar(52, open_="1.03121", close="1.03160", high="1.03176", low="1.03107"),
    bar(53, open_="1.03161", close="1.03164", high="1.03183", low="1.03131"),
    bar(54, open_="1.03165", close="1.03151", high="1.03176", low="1.03128"),
    bar(55, open_="1.03150", close="1.03089", high="1.03182", low="1.03084"),
    bar(56, open_="1.03089", close="1.03127", high="1.03166", low="1.03077"),
    bar(57, open_="1.03126", close="1.03272", high="1.03283", low="1.03099"),
    bar(58, open_="1.03272", close="1.03501", high="1.03524", low="1.03266"),
    bar(59, open_="1.03502", close="1.03555", high="1.03688", low="1.03401"),
    bar(60, open_="1.03556", close="1.03351", high="1.03580", low="1.03351"),
    bar(61, open_="1.03351", close="1.04219", high="1.04329", low="1.03286"),
    bar(62, open_="1.04216", close="1.04108", high="1.04307", low="1.03922"),
    bar(63, open_="1.04110", close="1.04167", high="1.04368", low="1.04044"),
    bar(64, open_="1.04168", close="1.03926", high="1.04237", low="1.03534"),
    bar(65, open_="1.03925", close="1.03934", high="1.03987", low="1.03713"),
    bar(66, open_="1.03936", close="1.03969", high="1.04029", low="1.03854"),
    bar(67, open_="1.03969", close="1.03874", high="1.03969", low="1.03792"),
    bar(68, open_="1.03874", close="1.03819", high="1.03892", low="1.03771"),
    bar(69, open_="1.03820", close="1.03883", high="1.03919", low="1.03788"),
    bar(70, open_="1.03883", close="1.03877", high="1.03920", low="1.03858"),
    bar(71, open_="1.03881", close="1.03886", high="1.03922", low="1.03861"),
    bar(72, open_="1.03888", close="1.03885", high="1.03917", low="1.03803"),
    bar(73, open_="1.03895", close="1.03834", high="1.03899", low="1.03816"),
    bar(74, open_="1.03835", close="1.03844", high="1.03853", low="1.03771"),
    bar(75, open_="1.03841", close="1.03811", high="1.03848", low="1.03761"),
    bar(76, open_="1.03809", close="1.03823", high="1.03854", low="1.03799"),
    bar(77, open_="1.03823", close="1.03928", high="1.03930", low="1.03801"),
    bar(78, open_="1.03927", close="1.03918", high="1.03944", low="1.03904"),
    bar(79, open_="1.03918", close="1.03968", high="1.04033", low="1.03909"),
    bar(80, open_="1.03968", close="1.03973", high="1.04042", low="1.03963"),
    bar(81, open_="1.03975", close="1.04146", high="1.04244", low="1.03975"),
    bar(82, open_="1.04146", close="1.04252", high="1.04252", low="1.04030"),
    bar(83, open_="1.04251", close="1.04276", high="1.04344", low="1.04241"),
    bar(84, open_="1.04263", close="1.04280", high="1.04345", low="1.04167"),
    bar(85, open_="1.04280", close="1.04134", high="1.04284", low="1.04052"),
    bar(86, open_="1.04134", close="1.03892", high="1.04171", low="1.03872"),
    bar(87, open_="1.03893", close="1.03965", high="1.04052", low="1.03866"),
    bar(88, open_="1.03965", close="1.03930", high="1.03975", low="1.03893"),
    bar(89, open_="1.03872", close="1.03727", high="1.03872", low="1.03552"),
    bar(90, open_="1.03729", close="1.03684", high="1.03815", low="1.03626"),
    bar(91, open_="1.03685", close="1.03670", high="1.03738", low="1.03538"),
    bar(92, open_="1.03670", close="1.03544", high="1.03694", low="1.03493"),
    bar(93, open_="1.03544", close="1.03553", high="1.03597", low="1.03522"),
    bar(94, open_="1.03551", close="1.03444", high="1.03560", low="1.03440"),
    bar(95, open_="1.03441", close="1.03393", high="1.03443", low="1.03384"),
    bar(96, open_="1.03378", close="1.03415", high="1.03424", low="1.03378"),
    bar(97, open_="1.03418", close="1.03415", high="1.03449", low="1.03391"),
    bar(98, open_="1.03416", close="1.03468", high="1.03494", low="1.03416"),
    bar(99, open_="1.03468", close="1.03530", high="1.03531", low="1.03453"),
    bar(100, open_="1.03530", close="1.03542", high="1.03549", low="1.03515"),
    bar(101, open_="1.03542", close="1.03478", high="1.03542", low="1.03462"),
    bar(102, open_="1.03477", close="1.03512", high="1.03521", low="1.03473"),
    bar(103, open_="1.03513", close="1.03550", high="1.03558", low="1.03494"),
    bar(104, open_="1.03549", close="1.03514", high="1.03575", low="1.03461"),
    bar(105, open_="1.03515", close="1.03324", high="1.03517", low="1.03284"),
    bar(106, open_="1.03322", close="1.03188", high="1.03396", low="1.03180"),
    bar(107, open_="1.03187", close="1.03195", high="1.03264", low="1.03154"),
    bar(108, open_="1.03192", close="1.03189", high="1.03214", low="1.03101"),
    bar(109, open_="1.03188", close="1.02977", high="1.03199", low="1.02732"),
    bar(110, open_="1.02978", close="1.02827", high="1.03002", low="1.02808"),
    bar(111, open_="1.02826", close="1.03034", high="1.03044", low="1.02809"),
    bar(112, open_="1.03035", close="1.02915", high="1.03075", low="1.02890"),
    bar(113, open_="1.02928", close="1.03016", high="1.03099", low="1.02926"),
    bar(114, open_="1.03016", close="1.03125", high="1.03145", low="1.02945"),
    bar(115, open_="1.03125", close="1.03053", high="1.03134", low="1.02982"),
    bar(116, open_="1.03053", close="1.03126", high="1.03159", low="1.03053"),
    bar(117, open_="1.03125", close="1.03090", high="1.03243", low="1.03083"),
    bar(118, open_="1.03093", close="1.03161", high="1.03167", low="1.03079"),
    bar(119, open_="1.03161", close="1.03170", high="1.03193", low="1.03139"),
    bar(120, open_="1.03079", close="1.03174", high="1.03174", low="1.03079"),
    bar(121, open_="1.03179", close="1.03132", high="1.03187", low="1.03132"),
    bar(122, open_="1.03132", close="1.03130", high="1.03147", low="1.03047"),
    bar(123, open_="1.03131", close="1.03143", high="1.03174", low="1.03114"),
    bar(124, open_="1.03144", close="1.03165", high="1.03199", low="1.03141"),
    bar(125, open_="1.03165", close="1.03110", high="1.03214", low="1.03109"),
    bar(126, open_="1.03110", close="1.03091", high="1.03122", low="1.03056"),
    bar(127, open_="1.03092", close="1.03067", high="1.03125", low="1.03053"),
    bar(128, open_="1.03067", close="1.03000", high="1.03082", low="1.02969"),
    bar(129, open_="1.03007", close="1.02910", high="1.03083", low="1.02876"),
    bar(130, open_="1.02909", close="1.03078", high="1.03078", low="1.02835"),
    bar(131, open_="1.03077", close="1.03058", high="1.03175", low="1.03032"),
    bar(132, open_="1.03060", close="1.03031", high="1.03125", low="1.02996"),
    bar(133, open_="1.03030", close="1.03011", high="1.03044", low="1.02968"),
    bar(134, open_="1.03011", close="1.03034", high="1.03051", low="1.02961"),
    bar(135, open_="1.03033", close="1.03080", high="1.03187", low="1.03010"),
    bar(136, open_="1.03080", close="1.03074", high="1.03148", low="1.03051"),
    bar(137, open_="1.03074", close="1.02994", high="1.03096", low="1.02948"),
    bar(138, open_="1.02993", close="1.02973", high="1.03011", low="1.02910"),
    bar(139, open_="1.02973", close="1.02974", high="1.03019", low="1.02937"),
    bar(140, open_="1.02972", close="1.03001", high="1.03025", low="1.02940"),
    bar(141, open_="1.03001", close="1.02980", high="1.03021", low="1.02978"),
    bar(142, open_="1.02981", close="1.03011", high="1.03037", low="1.02981"),
    bar(143, open_="1.03012", close="1.02995", high="1.03012", low="1.02991"),
    bar(144, open_="1.02992", close="1.03005", high="1.03006", low="1.02956"),
    bar(145, open_="1.03012", close="1.02964", high="1.03019", low="1.02955"),
    bar(146, open_="1.02965", close="1.02954", high="1.03004", low="1.02921"),
    bar(147, open_="1.02952", close="1.03020", high="1.03046", low="1.02936"),
    bar(148, open_="1.03020", close="1.03019", high="1.03038", low="1.02982"),
    bar(149, open_="1.03017", close="1.02984", high="1.03033", low="1.02976"),
    bar(150, open_="1.02983", close="1.02949", high="1.03000", low="1.02935"),
    bar(151, open_="1.02951", close="1.02855", high="1.02974", low="1.02846"),
    bar(152, open_="1.02856", close="1.02818", high="1.02884", low="1.02811"),
    bar(153, open_="1.02816", close="1.02970", high="1.02972", low="1.02812"),
    bar(154, open_="1.02971", close="1.03026", high="1.03119", low="1.02927"),
    bar(155, open_="1.03026", close="1.02999", high="1.03058", low="1.02990"),
    bar(156, open_="1.03001", close="1.03022", high="1.03059", low="1.02998"),
    bar(157, open_="1.03020", close="1.03018", high="1.03053", low="1.02989"),
    bar(158, open_="1.03017", close="1.03055", high="1.03094", low="1.02991"),
    bar(159, open_="1.03054", close="1.02565", high="1.03118", low="1.02128"),
    bar(160, open_="1.02565", close="1.02532", high="1.02728", low="1.02444"),
    bar(161, open_="1.02531", close="1.02504", high="1.02782", low="1.02363"),
    bar(162, open_="1.02502", close="1.02375", high="1.02512", low="1.02274"),
    bar(163, open_="1.02375", close="1.02445", high="1.02506", low="1.02328"),
    bar(164, open_="1.02444", close="1.02415", high="1.02460", low="1.02358"),
    bar(165, open_="1.02416", close="1.02453", high="1.02489", low="1.02350"),
    bar(166, open_="1.02454", close="1.02445", high="1.02460", low="1.02371"),
    bar(167, open_="1.02444", close="1.02436", high="1.02489", low="1.02426"),
    bar(168, open_="1.02316", close="1.02454", high="1.02455", low="1.02316"),
    bar(169, open_="1.02464", close="1.02431", high="1.02468", low="1.02387"),
    bar(170, open_="1.02432", close="1.02424", high="1.02450", low="1.02365"),
    bar(171, open_="1.02425", close="1.02417", high="1.02499", low="1.02392"),
    bar(172, open_="1.02417", close="1.02401", high="1.02452", low="1.02386"),
    bar(173, open_="1.02399", close="1.02159", high="1.02404", low="1.02127"),
    bar(174, open_="1.02154", close="1.02118", high="1.02200", low="1.02075"),
)


def a_structure_market() -> list[Candle]:
    """The measured window above, as the loop wants it."""
    return list(ARMS_A_RESTING_LIMIT)


# --------------------------------------------------------------------------- #
# The thing that broke: a real setup crossing the line                          #
# --------------------------------------------------------------------------- #


def test_a_resting_order_survives_the_hand_over() -> None:
    """The whole point. 35% to 73% of bars leave one resting, measured on EURUSD H1 — so an
    order that does not cross is a region the session silently never trades."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.resting(), "the scenario armed nothing; it cannot show a hand-over"

    live = a_broker()
    result = carry(warmed, live)

    assert result.carried, "the resting order did not cross"
    assert result.refused == ()
    assert [order.client_id for order in live.resting()] == [
        order.client_id for order in warmed.resting()
    ]


def test_the_strategy_and_the_live_broker_agree_after_the_hand_over() -> None:
    """The ghost, stated directly: the strategy believes it placed an order, so the broker must
    be holding one. This is the assertion the vetoing design failed, four times in five."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    live = a_broker()
    carry(warmed, live)

    armed = getattr(strategy, "_armed", None)
    believes_placed = armed is not None and armed.placed
    assert believes_placed, "the scenario never armed anything; nothing to disagree about"
    assert live.resting(), "the strategy believes it placed an order the broker never got"


def test_the_money_does_not_cross() -> None:
    """The other half of the bargain. Warm-up is a backtest, so it moves the account — and the
    session must start at its initial capital regardless of how that backtest went."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    assert warmed.account().equity != CAPITAL, (
        "the warm-up did not move the account, so 'it did not cross' and 'it crossed an "
        "identical number' are the same fact and this test proves neither"
    )

    live = a_broker()
    carry(warmed, live)

    assert live.account().balance == CAPITAL
    assert live.account().equity == CAPITAL
    assert live.trades() == ()


def test_warm_up_really_does_trade() -> None:
    """⚠️ The separating test. Everything above is satisfied by a warm-up that did nothing at
    all — and the design before this one did exactly that, on purpose. If history stops producing
    fills, these tests stop meaning anything and this is the one that says so."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)

    assert warmed.trades(), "history closed no round trip; the design's premise is untested"
    assert warmed.resting(), "history left nothing armed; the hand-over has nothing to carry"
    assert warmed.account().equity != CAPITAL, "the account never moved"


# --------------------------------------------------------------------------- #
# What a hand-over refuses                                                      #
# --------------------------------------------------------------------------- #


def test_a_session_cannot_open_mid_trade() -> None:
    """Inheriting a position would report a trade the session never took, entered before it
    existed. Measured at 0.4%-3% of bars on EURUSD H1 — rare enough to refuse."""
    strategy = ScriptedStrategy(script={2: [_entry_with_stop()]})
    warmed = warm(rising(8), strategy)
    assert warmed.positions("EURUSD"), "the scenario did not end holding a position"

    with pytest.raises(EngineError, match="cannot open mid-trade"):
        carry(warmed, a_broker(), bars=8)


def test_a_used_live_broker_is_refused() -> None:
    """Handed a broker that already traded, `hand_over` is being called twice — and the second
    call would quietly double the resting orders."""
    strategy = ScriptedStrategy(script={2: [_entry_with_stop()], 5: [_close_out()]})
    used = warm(rising(8), strategy)
    assert used.trades(), "the scenario left no trade; it cannot show a used broker"

    with pytest.raises(EngineError, match="not empty"):
        carry(a_broker(), used, bars=8)


def test_a_broker_that_cannot_list_its_orders_carries_nothing() -> None:
    """`resting()` is not on the `Broker` protocol. A broker without it must carry nothing —
    visibly wrong — rather than raise `AttributeError` on start-up."""
    warmed = ImmediateFillBroker(costs=Decimal(0))

    result = carry(warmed, a_broker(), bars=0)

    assert result.carried == ()
    assert result.refused == ()
    assert not hasattr(warmed, "resting"), "the double grew a resting(); the test is now vacuous"


def test_what_the_live_broker_refuses_is_reported_not_raised() -> None:
    """The session is otherwise fine, and the operator needs to know *which* region will not be
    traded — an exception would replace that with a stack trace."""

    class RefusesEverything(BacktestBroker):
        def submit(self, order: OrderRequest) -> OrderResult:
            return OrderResult(order=order, accepted=False, reason="no")

    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.resting()

    live = RefusesEverything(instrument=EURUSD, initial_capital=CAPITAL, cost_model=NoCostModel())
    result = carry(warmed, live, bars=10)

    assert result.carried == ()
    assert result.refused, "a refusal happened and was not reported"


def test_the_hand_over_records_the_bars_it_was_told() -> None:
    """A fact a session stores. Nothing derives it, because only the caller knows how much
    history it actually found — a window can come back short."""
    result = carry(a_broker(), a_broker(), bars=417)

    assert result.bars == 417


def test_a_carried_order_is_resized_against_the_session_account() -> None:
    """The money blocker, stated as the two numbers that differ.

    `volume` is the one field on an order that is not a fact about the market: a
    `PercentRiskManager` computed it from the equity of the ledger this hand-over throws away.
    Measured on this window, the warm-up ends at 9 901 and the order it leaves resting carries
    **1.08** lots; the session's own account is 10 000 and calls for **1.09**. A 1% drift in an
    account nobody has becomes a 1% drift in the risk of the session's first trade.

    ⚠️ One percent is a small gap on purpose — it is what this window actually produces, and a
    test written against a comfortable gap would not have caught the rounding case. On a warm-up
    that ran 10 000 to 13 000 the same order sizes at 1.42 against 1.09.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    original = warmed.resting()[0]
    assert original.volume == Decimal("1.08"), "the window changed; re-measure before editing"

    live = a_broker()
    result = carry(warmed, live)

    assert result.carried[0].volume == Decimal("1.09"), "the order kept the discarded ledger's size"
    assert live.resting()[0].volume == Decimal("1.09")
    assert result.carried[0].client_id == original.client_id, "it stopped being the same order"
    assert result.carried[0].limit_price == original.limit_price
    assert result.carried[0].decided_at == original.decided_at, (
        "the decision instant moved; `loop._reject_lookahead` is armed by it, so a stamp "
        "refreshed to the hand-over would let the first live bar fill a decision made on it"
    )
    # ⚠️ The snapshot is the only one that will ever exist for this order. The live broker is
    # fresh, so its bar window cannot rebuild one — `_snapshot_through` would find nothing and
    # hand back the arming window as it came. Dropped here, the session's first trade charts
    # with no context and nothing complains.
    assert result.carried[0].snapshot == original.snapshot


def test_an_order_that_resizes_to_nothing_does_not_cross() -> None:
    """The risk manager saying "not this trade" on the session's own terms. Carrying it anyway
    would overrule the one component whose job is to say no."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    resting = warmed.resting()[0]

    live = a_broker()
    result = hand_over(
        warmed,
        live,
        symbol="EURUSD",
        bars=175,
        risk=_NeverSizes(),
        instrument=EURUSD,
    )

    assert result.carried == ()
    assert result.refused == (resting.client_id,)
    assert live.resting() == ()


def test_handing_over_twice_is_refused() -> None:
    """⚠️ The guard the first version missed. A successful hand-over leaves no position and no
    trade — it leaves an **order**, which is exactly what a `positions or trades` check cannot
    see. Without this the second call passes the guard, and the only thing stopping a duplicate
    is `BacktestBroker` refusing a repeated `client_id` — reported as "this region will not be
    traded", which is a lie, because it is resting. A venue that does not deduplicate names
    would end up holding two limits on one zone.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    live = a_broker()
    assert carry(warmed, live).carried, "the first hand-over carried nothing"

    with pytest.raises(EngineError, match="not empty"):
        carry(warmed, live)

    assert len(live.resting()) == 1, "the second hand-over duplicated the order"


def test_a_live_broker_holding_a_position_is_refused() -> None:
    """The other clause of the same guard, which no test reached: a broker can hold a position
    without having closed a trade, and that one is not fresh either."""
    holding = warm(rising(8), ScriptedStrategy(script={2: [_entry_with_stop()]}))
    assert holding.positions("EURUSD")
    assert holding.trades() == (), "it closed a trade, so this exercises the other clause"

    with pytest.raises(EngineError, match="not empty"):
        carry(a_broker(), holding, bars=8)


def test_the_hand_over_records_what_the_warm_up_traded() -> None:
    """Recorded, never carried. The strategy crosses holding `_traded` for zones whose trades
    exist only in the discarded ledger, so "why did the session skip this region?" has an answer
    that lives nowhere in the session. This number is the smallest honest trace of it."""
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    assert warmed.trades(), "no trade to record"

    result = carry(warmed, a_broker())

    assert result.warm_trades == len(warmed.trades())
    assert result.warm_trades > 0


def test_every_resting_order_crosses_not_just_the_first() -> None:
    """`hand_over` loops, and a loop that carried only the head would pass every other test here
    — the setup has a single `_armed` slot, so two resting orders are unreachable through it.
    A double with two is the only way to hold the loop to what it is written as."""
    warmed = a_broker()
    first = _a_limit(client_id="zone-a", limit="1.03000")
    second = _a_limit(client_id="zone-b", limit="1.02500")
    assert warmed.submit(first).accepted
    assert warmed.submit(second).accepted
    assert len(warmed.resting()) == 2

    live = a_broker()
    result = carry(warmed, live)

    assert [order.client_id for order in result.carried] == ["zone-a", "zone-b"]
    assert len(live.resting()) == 2


class _NeverSizes:
    """A risk manager that always answers zero — "no trade", in the loop's own vocabulary."""

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Decimal:
        return Decimal(0)

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        return True


def _a_limit(*, client_id: str, limit: str) -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal("1"),
        decided_at=START,
        stop_loss=Decimal(limit) - Decimal("0.00500"),
        reason="entry.test",
        limit_price=Decimal(limit),
        client_id=client_id,
    )


def test_the_risk_manager_can_still_veto_a_carried_order() -> None:
    """⚠️ `allow` as well as `size`, and the split is what `protocols.py` insists on.

    Sizing is arithmetic; `allow` is the veto, and the veto is where a kill switch or a daily
    loss limit will live. A hand-over that asked only `size` would have made the inversion the
    protocol exists to prevent — the arithmetic deciding whether an order exists. Today nothing
    refuses, so this is the only place that says the question is asked at all.
    """
    strategy = build_setup({"type": "structure_choch"})
    warmed = warm(a_structure_market(), strategy)
    resting = warmed.resting()[0]

    live = a_broker()
    result = hand_over(
        warmed, live, symbol="EURUSD", bars=175, risk=_VetoesEverything(), instrument=EURUSD
    )

    assert result.carried == ()
    assert result.refused == (resting.client_id,)
    assert live.resting() == (), "a vetoed order reached the broker"


class _VetoesEverything:
    """Sizes normally and refuses everything — a kill switch, in the shape one will have."""

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Decimal:
        return PercentRiskManager(percent=Decimal(1)).size(signal, account, instrument)

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        return False


# --------------------------------------------------------------------------- #
# Reading what is still cold                                                    #
# --------------------------------------------------------------------------- #


class Charting:
    """A strategy that charts two averages of different periods and trades nothing."""

    def __init__(self) -> None:
        self._fast = EMA(period=3)
        self._slow = SMA(period=9)

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        self._fast.update(context.candle)
        self._slow.update(context.candle)
        return ()

    def overlays(self) -> dict[str, EMA | SMA]:
        return {"fast": self._fast, "slow": self._slow}


def test_unwarmed_indicators_names_them_in_drawing_order() -> None:
    """Two overlays with different periods, so order is observable: with one, a reversed tuple
    and a correct one are the same tuple."""
    strategy = Charting()

    assert unwarmed_indicators(strategy) == ("fast", "slow")

    for candle in rising(3):
        strategy.on_bar(a_context(candle))

    assert unwarmed_indicators(strategy) == ("slow",)

    for candle in rising(9):
        strategy.on_bar(a_context(candle))

    assert unwarmed_indicators(strategy) == ()


def test_a_strategy_that_charts_nothing_reports_nothing_cold() -> None:
    """⚠️ Empty here means "nothing to warm", not "everything warm". A caller has to ask
    `isinstance(strategy, Charted)` to tell them apart; this pins the behaviour so nobody reads
    the empty tuple as a clean bill of health."""
    assert unwarmed_indicators(ScriptedStrategy(script={})) == ()


def test_reading_the_overlays_does_not_advance_them() -> None:
    """`Charted.overlays` hands back live objects. Asking twice must not warm anything."""
    strategy = Charting()
    for candle in rising(2):
        strategy.on_bar(a_context(candle))

    first = unwarmed_indicators(strategy)
    assert first == ("fast", "slow")
    assert unwarmed_indicators(strategy) == first
    assert unwarmed_indicators(strategy) == first, "reading it warmed it"


# --------------------------------------------------------------------------- #


def _entry_with_stop() -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09000"),
        reason="test.entry",
    )


def _close_out() -> Signal:
    return Signal(
        kind=SignalKind.EXIT,
        side=Side.LONG,
        reference_price=Decimal("1.10100"),
        reason="test.exit",
    )
