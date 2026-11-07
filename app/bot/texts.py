"""Тексты бота. Собраны в одном месте, чтобы тон был одинаковый везде."""

from __future__ import annotations

from datetime import datetime

from app.analytics.insights import Insight
from app.analytics.reports import PeriodReport
from app.classify import categories as cats
from app.models import Money, ReviewItem, fmt_money
from app.service import BatchTrace

BAR_FULL = "█"
BAR_EMPTY = "░"


def bar(share: float, width: int = 10) -> str:
    filled = max(0, min(width, round(share * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


# Первый экран — это кнопка, а не текст. Всё, что человек делает дальше,
# живёт в приложении, поэтому /start только называет продукт и уступает
# место кнопке; команды и методика остались в /help, за ними идут потом.
START = """<b>Честный месяц</b> — считает, куда на самом деле уходят деньги.
Открывайте приложение кнопкой ниже. /help — что я умею."""

# Кнопки не будет: без адреса приложения она открывала бы пустоту. Тогда
# /start обязан сказать словами, что делать, а не молчать сломанной кнопкой.
START_NO_MINIAPP = """<b>Честный месяц</b> — считает, куда на самом деле уходят деньги.
Приложение не настроено: задайте MINIAPP_URL в .env и перезапустите сервис.
Операции пока можно присылать сюда — /help."""

HELP = """<b>Что я умею</b>

<b>Источники данных</b>
• CSV-выписка Т-Банка, Сбербанка, Альфы — просто пришлите файл
• скриншот списка операций из приложения банка или фотография чека
• голосовое или текст для наличных: «такси 450», «кофе 300»

Один и тот же платёж, присланный скриншотом и потом пришедший в выписке,
я засчитаю один раз.

<b>Что я считаю правильно</b>
• перевод между своими картами — не расход и не доход
• пополнение кошелька маркетплейса — перевод, расход появится при покупке
• выдача и возврат долга, в том числе частями
• счёт за компанию — в расходы попадает только ваша доля
• возврат товара — минус к трате, а не доход
• снятие наличных — трата только тогда, когда известно, на что

<b>Отчёты</b>
/today /week /month /year — итоги за период
/questions — спорные операции, по одному тапу на каждую
/streak — серия дней
/checkup — проверка сходимости цифр

<b>Отправка с телефона</b>
/link (он же /связь) — личная ссылка для быстрой команды iPhone.
Файлы со всех телефонов приходят на один адрес, и опознаёт вас не
telegram id, а этот ключ: id видит каждый, кому вы писали, а ключ —
только вы. Там же кнопка «Выпустить новую», если ключ куда-то утёк."""


def money_line(label: str, amount: Money, *, sign: bool = False, width: int = 16) -> str:
    return f"{label}{fmt_money(amount, sign=sign):>{width}}"


def report_message(
    report: PeriodReport, *, title: str | None = None, wallets: list | None = None
) -> str:
    wallets = wallets or []
    period = report.period
    lines = [f"<b>{title or period.label}</b>", ""]

    lines.append(f"Потрачено: <b>{fmt_money(report.spend_total)}</b>")
    if report.prev_spend_total:
        delta = report.spend_delta
        arrow = "▲" if delta > 0 else "▼"
        pct = abs(delta) / report.prev_spend_total * 100
        lines.append(
            f"{arrow} {fmt_money(abs(delta))} ({pct:.0f}%) к прошлому периоду"
        )
    lines.append(f"Получено: {fmt_money(report.income_total)}")
    lines.append(f"В среднем в день: {fmt_money(report.avg_per_day)}")
    lines.append("")

    f = report.forecast
    if f is not None and f.is_meaningful:
        lines.append(
            f"<i>К концу периода при таком темпе: {fmt_money(f.projected)}"
            f" ({fmt_money(f.daily_rate)} в день)</i>"
        )
    lines.append("")

    awareness = report.awareness
    lines.append(f"<b>Осознанность: {awareness.percent}%</b>  {bar(awareness.score)}")
    if awareness.unclear_volume:
        lines.append(
            f"<i>непонятных денег: {fmt_money(awareness.unclear_volume)}</i>"
        )
    lines.append("")

    if report.categories:
        lines.append("<b>Куда ушли</b>")
        for line in report.categories[:8]:
            change = ""
            if line.prev_amount and line.delta_pct is not None and abs(line.delta_pct) >= 10:
                change = f"  {'▲' if line.delta > 0 else '▼'}{abs(line.delta_pct):.0f}%"
            lines.append(
                f"{line.emoji} {line.title} — <b>{fmt_money(line.amount)}</b>"
                f"  {line.share * 100:.0f}%{change}"
            )
        if len(report.categories) > 8:
            rest = sum(item.amount for item in report.categories[8:])
            lines.append(f"… и ещё {len(report.categories) - 8} категорий на {fmt_money(rest)}")
        lines.append("")

    if report.spend_total:
        share = report.optional_total / report.spend_total
        lines.append(
            f"Обязательные: {fmt_money(report.essential_total)} · "
            f"необязательные: {fmt_money(report.optional_total)} ({share * 100:.0f}%)"
        )

    for trace in wallets:
        lines.append("")
        parts = ", ".join(
            f"{cats.title(code)} {fmt_money(amount)}"
            for code, amount in sorted(
                trace.spent_by_category.items(), key=lambda kv: -kv[1]
            )
        )
        lines.append(
            f"<b>{trace.name}</b>: завели {fmt_money(trace.topped_up)} → {parts}"
            + (f", остаток {fmt_money(trace.balance)}" if trace.balance else "")
        )

    if report.debts_owed_to_me or report.debts_i_owe:
        lines.append("")
        if report.debts_owed_to_me:
            lines.append(f"Вам должны: {fmt_money(report.debts_owed_to_me)}")
        if report.debts_i_owe:
            lines.append(f"Вы должны: {fmt_money(report.debts_i_owe)}")

    if report.open_questions:
        lines.append("")
        lines.append(
            f"Спорных операций: <b>{report.open_questions}</b> — /questions"
        )
    return "\n".join(lines)


def weeks_message(rows: list[tuple[str, Money]], total: Money) -> str:
    lines = ["<b>По неделям</b>", ""]
    peak = max((amount for _, amount in rows), default=0)
    for label, amount in rows:
        share = (amount / peak) if peak else 0
        lines.append(f"<code>{bar(share, 8)}</code> {label} — {fmt_money(amount)}")
    lines.append("")
    lines.append(f"Сумма недель: <b>{fmt_money(sum(a for _, a in rows))}</b>")
    lines.append(f"Итог месяца:  <b>{fmt_money(total)}</b>")
    return "\n".join(lines)


def insights_message(items: list[Insight], saving: Money) -> str:
    if not items:
        return "Пока нечего посоветовать — данных мало."
    lines = ["<b>Что с этим делать</b>", ""]
    for item in items[:5]:
        tag = f"  <i>−{fmt_money(item.saving)}/мес</i>" if item.has_saving else ""
        lines.append(f"• <b>{item.title}</b>{tag}")
        lines.append(f"  {item.detail}")
        lines.append("")
    if saving:
        lines.append(f"Итого можно вернуть примерно <b>{fmt_money(saving)}</b> в месяц.")
    return "\n".join(lines)


def question_message(item: ReviewItem, *, left: int) -> str:
    head = f"<b>Вопрос</b>  <i>(осталось {left})</i>" if left else "<b>Вопрос</b>"
    body = [head, "", item.question]
    if item.operation is not None and item.operation.explanation:
        body += ["", f"<i>{item.operation.explanation}</i>"]
    return "\n".join(body)


def import_message(
    *,
    source: str,
    added: int,
    duplicates: int,
    superseded: int,
    questions: int,
    trace: BatchTrace | None = None,
) -> str:
    lines = [f"<b>{source}</b>", "", f"Добавлено операций: <b>{added}</b>"]
    if duplicates:
        lines.append(f"Пропущено дублей: {duplicates}")
    if superseded:
        lines.append(f"Уточнено по выписке: {superseded}")
    if trace is not None:
        lines.append(trace_line(trace))
    lines.append("")
    if questions:
        lines.append(
            f"Разобрал всё, кроме <b>{questions}</b> спорных операций. "
            f"Это {questions} тап{plural(questions, '', 'а', 'ов')} — /questions"
        )
    else:
        lines.append("Разобрал всё, вопросов нет.")
    return "\n".join(lines)


def streak_message(days: int, best: int, per_day: float) -> str:
    return (
        f"<b>Серия: {days} дн.</b>\n"
        f"Рекорд: {best} дн.\n\n"
        f"Действий в день в среднем: <b>{per_day:.1f}</b>\n"
        f"<i>Считается по факту, а не обещается.</i>"
    )


def checkup_message(problems: list[str], weeks_ok: bool) -> str:
    if not problems and weeks_ok:
        return (
            "<b>Цифры сходятся</b>\n\n"
            "• каждая операция сбалансирована\n"
            "• книга в целом сходится в ноль\n"
            "• недельные срезы складываются в месячный\n"
            "• ни один перевод или долг не попал в расходы"
        )
    lines = ["<b>Найдены расхождения</b>", ""]
    if not weeks_ok:
        lines.append("• сумма недель не равна месяцу")
    lines += [f"• {p}" for p in problems[:10]]
    return "\n".join(lines)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 тап, 2 тапа, 5 тапов."""
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def trace_line(trace: BatchTrace) -> str:
    """Что дала именно эта загрузка.

    Без этой строки «скинул картинку» заканчивается общими числами по всей
    книге, и человек не знает, что распозналось в том, что он прислал.
    """
    parts = [
        f"{trace.operations} "
        f"{plural(trace.operations, 'операция', 'операции', 'операций')}"
    ]
    if trace.questions:
        parts.append(
            f"{trace.questions} "
            f"{plural(trace.questions, 'вопрос', 'вопроса', 'вопросов')}"
        )
    if trace.duplicates:
        # «Дубли» — слово про нашу кухню. Человеку важно другое: эти
        # операции у него уже есть, поэтому их и не добавилось.
        parts.append(f"уже было {trace.duplicates}")
    return f"<i>Из этого файла: {', '.join(parts)} · загрузка №{trace.batch_id}</i>"


def upload_link_message(
    *, url: str, token: str, issued_at: datetime | None, fresh: bool = False
) -> str:
    """Личная ссылка для быстрой команды.

    Адрес и ключ — отдельными строками, а не одной готовой ссылкой: адрес
    общий для всех, а ключ личный, и человек должен видеть разницу. Заодно
    ключ не уезжает в предпросмотр ссылки, когда сообщение пересылают.
    """
    head = (
        "<b>Новая ссылка выпущена</b>\n\nСтарая больше не работает."
        if fresh
        else "<b>Ваша ссылка для отправки файлов</b>"
    )
    when = f"\nВыпущена: {issued_at:%d.%m.%Y %H:%M}" if issued_at else ""
    return (
        f"{head}\n\n"
        # Короткая строка про цену — над ключом, а не под инструкцией:
        # человек, который делает скриншот верхней части сообщения, чтобы
        # переслать «вот мой адрес», нижние абзацы в кадр не возьмёт.
        "Ниже личный ключ. Кто его увидит — сможет писать в вашу книгу.\n\n"
        f"Адрес: <code>{url}</code>\n"
        # Ключ под спойлером: так он не попадает ни в превью чата в списке
        # диалогов, ни в пуш на заблокированном экране — то есть «показать
        # его на экране» больше не происходит без участия человека.
        f"Ключ: <tg-spoiler><code>{token}</code></tg-spoiler>\n"
        f"{when}\n\n"
        "В «Быстрых командах» на iPhone: действие «Получить содержимое "
        "URL-адреса», способ POST, адрес выше, в разделе «Заголовки» — "
        f"<code>X-Upload-Token</code> со значением ключа, тело — файл. "
        "Назначьте команду на двойное касание задней панели — и скриншот "
        "будет уезжать сюда одним касанием.\n\n"
        "<b>Ключ личный.</b> Кто его получит, сможет добавлять операции в "
        "вашу книгу и испортить ваши цифры. Не пересылайте его и не "
        "показывайте на экране. Если ключ всё-таки увидели — «Выпустить "
        "новую»: старая ссылка перестанет работать в ту же секунду."
    )


def group_notice(bot_link: str) -> str:
    """Ответ в общем чате: ни денег, ни ключа здесь не будет.

    Отказ обязан объяснять причину и показывать дорогу, иначе он читается
    как поломка: человек набрал команду и не получил ничего.
    """
    tail = f"\n\nНапишите мне сюда: {bot_link}" if bot_link else ""
    return (
        "Здесь я молчу. Итоги и личный ключ загрузки — это ваши деньги и "
        "ваш доступ, в общем чате их видят все участники, а кнопки под "
        f"такими сообщениями нажимает кто угодно.{tail}"
    )


def quick_entry_ack(label: str, amount: Money, category: str | None) -> str:
    return (
        f"Записал: <b>{label}</b> — {fmt_money(amount)}\n"
        f"Категория: {cats.label(category)}\n"
        f"<i>Списано с наличных.</i>"
    )
