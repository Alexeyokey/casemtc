"""One synthetic consultation for testing the web and CSV adapters."""

from consultation import Reply, Turn, parse_csv_history


METERING = "demo:tenant-metering"
PHOTO_READING = "demo:photo-reading"


def consult(history: tuple[Turn, ...]) -> Reply:
    if not history or history[-1].role != "user":
        raise ValueError("Для консультации нужна новая реплика пользователя.")

    latest = history[-1].text.casefold().replace("ё", "е")
    user_text = "\n".join(turn.text for turn in history if turn.role == "user")
    facts = user_text.casefold().replace("ё", "е")
    tenant_metering = "арендатор" in facts and "счетчик" in facts
    mixed_collection = "фото" in facts and any(word in facts for word in ("техник", "вручную"))
    declines_contact = "телефон" in latest and any(
        phrase in latest for phrase in ("не остав", "не хочу", "не буду", "не готов", "без телефон")
    )
    asks_for_contact = any(
        phrase in latest
        for phrase in ("свяжитесь", "перезвоните", "связаться со специалистом", "поговорить со специалистом")
    )

    if asks_for_contact and not declines_contact:
        summary = "Клиент просит связаться со специалистом. Уточнить задачу на первом звонке."
        if tenant_metering:
            summary = "Клиент хочет автоматизировать учёт по арендаторам. Счётчики уже установлены."
            if mixed_collection:
                summary += " Показания собирают по фото и вручную. Уточнить модели приборов и число точек ручного сбора."
        return Reply("Оставьте телефон для связи со специалистом.", "offer_contact", summary)

    if not tenant_metering:
        return Reply(
            "Расскажите, какой учёт нужен и какие счётчики уже установлены.", "clarify"
        )

    if not mixed_collection:
        return Reply(
            "Начните с перечня счётчиков по арендаторам. Как сейчас собираете показания?",
            "clarify",
            evidence_ids=(METERING,),
        )

    prefix = "Можем продолжить без телефона. " if declines_contact else ""
    return Reply(
        prefix + "Начните с перечня счётчиков по арендаторам. Для совместимых приборов "
        "можно рассмотреть автоматический сбор. Для приборов на ручном сборе стоит проверить, "
        "подходит ли распознавание показаний по фото. Сколько счётчиков техник обходит вручную?",
        "answer",
        evidence_ids=(METERING, PHOTO_READING),
    )


async def answer_question(question: str) -> str:
    return consult(parse_csv_history(question)).text


if __name__ == "__main__":
    from solution import main

    raise SystemExit(main(answer_question=answer_question))
