"""
Turns raw tutor material (Kazakh<->Russian word lists, texts) into draft
Word + Task records via Claude. Uses forced tool-use so the response is
guaranteed to match our schema instead of hoping the model returns clean JSON.
"""

import anthropic

from app.config import settings

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

MODEL = "claude-sonnet-5"

TASK_TYPE_ENUM = [
    "translate_kk_ru",
    "translate_ru_kk",
    "flashcard",
    "fill_blank",
    "multiple_choice",
    "anagram",
    "listening_test",
    "fill_in_the_blank",
    "true_false",
    "sentence_builder",
]

CONTENT_DESCRIPTION = (
    "translate_kk_ru/translate_ru_kk: {prompt, answer}. "
    "flashcard: {front, answer} where answer is the translation shown "
    "on the back of the card. "
    "fill_blank: {sentence with '__' marking the missing letters, answer}. "
    "multiple_choice: {prompt, options (4 strings, in Kazakh or Russian matching prompt language), answer}. "
    "anagram: {answer} where answer is the Kazakh word EXACTLY as spelled in "
    "word_kazakh (letters get shuffled client-side, don't shuffle them yourself). "
    "listening_test: {word_kazakh, options (4 Russian translations, one correct plus "
    "3 plausible wrong ones), answer (the correct Russian translation, must be one of options)}. "
    "fill_in_the_blank: {sentence (a short natural Kazakh sentence using the word in its "
    "correctly declined/conjugated form, with that word replaced by '___'), "
    "options (4 candidate word forms, one correct plus 3 plausible wrong forms), "
    "answer (the correct option, must match one of options exactly), "
    "grammar_case (OPTIONAL: a short human-readable label in Russian for the case/form "
    "being practiced, e.g. 'дательный падеж (барыс)' - only include it when the sentence "
    "clearly hinges on a specific case/form, omit the field entirely otherwise)}. "
    "true_false: {kazakh, shown_translation (a Russian translation that is correct about "
    "half the time and a plausible wrong translation the other half), "
    "answer ('true' if shown_translation is correct, else 'false')}. "
    "sentence_builder: {answer (a short natural Kazakh sentence, 3-6 words, correct Kazakh "
    "word order, that uses the word - words get shuffled client-side), "
    "grammar_case (OPTIONAL, same convention as fill_in_the_blank)}."
)

GENERATE_TASKS_TOOL = {
    "name": "submit_generated_tasks",
    "description": "Submit the vocabulary words and exercises extracted from the tutor's material.",
    "input_schema": {
        "type": "object",
        "properties": {
            "words": {
                "type": "array",
                "description": "Every distinct Kazakh<->Russian word/phrase pair found in the text.",
                "items": {
                    "type": "object",
                    "properties": {
                        "kazakh": {"type": "string"},
                        "russian": {"type": "string"},
                        "part_of_speech": {
                            "type": "string",
                            "description": "e.g. noun, verb, adjective. Best guess, in Russian.",
                        },
                    },
                    "required": ["kazakh", "russian"],
                },
            },
            "tasks": {
                "type": "array",
                "description": "Exercises generated from the words above, several types per word where sensible.",
                "items": {
                    "type": "object",
                    "properties": {
                        "word_kazakh": {
                            "type": "string",
                            "description": "Kazakh form of the word this task is based on, must match one entry in `words`.",
                        },
                        "type": {
                            "type": "string",
                            "enum": TASK_TYPE_ENUM,
                        },
                        "content": {
                            "type": "object",
                            "description": CONTENT_DESCRIPTION,
                        },
                    },
                    "required": ["word_kazakh", "type", "content"],
                },
            },
        },
        "required": ["words", "tasks"],
    },
}

SYSTEM_PROMPT = """Ты помогаешь готовить учебные материалы по казахскому языку для ребёнка,
который изучает его как второй язык. Тебе дают текст от репетитора: это может быть список
слов (казахский - русский), связный текст или упражнение.

Твоя задача:
1. Извлечь все пары слов/словосочетаний казахский<->русский.
2. Для КАЖДОГО слова сгенерировать упражнения ВСЕХ подходящих типов — не выбирай 2-4 "на вкус",
   старайся охватить максимум типов на каждое слово, чтобы ни одно слово не осталось с бедным
   набором заданий. Типы и когда их использовать:
   - flashcard, translate_kk_ru, translate_ru_kk, multiple_choice, anagram, listening_test,
     true_false — применимы почти всегда, генерируй их для каждого слова:
     - multiple_choice — три неправильных варианта должны быть похожими по смыслу или форме
       словами, а не случайными.
     - anagram — не бери слишком длинные/составные слова (>8 букв) или словосочетания из
       нескольких слов, для них анаграмма не подходит.
     - listening_test — 3 отвлекающих варианта перевода должны быть правдоподобными (похожая
       тема или звучание).
     - true_false — показывай shown_translation неверным примерно в половине случаев (не всегда
       верным и не всегда неверным), иначе задание становится бессмысленным.
   - fill_blank — для слов длиннее 3 букв; пропускай 1-3 буквы внутри слова, не весь корень.
   - fill_in_the_blank и sentence_builder — генерируй ТОЛЬКО если можешь придумать короткое
     естественное предложение (для fill_in_the_blank — где слово стоит в правильной падежной/
     спряжённой форме). Если естественное предложение не придумывается — просто пропусти именно
     эти два типа для этого слова, не выдумывай неестественные фразы ради галочки. Когда
     предложение явно демонстрирует конкретный падеж — заполни поле grammar_case, иначе не
     добавляй это поле.
3. Для длинных/абстрактных слов (как "достижение", "ответственность") multiple_choice особенно
   важен — их сложнее угадать вслепую.
4. Не выдумывай слов, которых нет в тексте.
5. Поле "words" ОБЯЗАТЕЛЬНО должно содержать все найденные пары — даже если это дублирует
   информацию, уже присутствующую в "tasks". Никогда не оставляй "words" пустым массивом,
   если в тексте есть хотя бы одно слово.

Вызови инструмент submit_generated_tasks с результатом."""


def generate_tasks_from_text(raw_text: str) -> dict:
    """Returns {"words": [...], "tasks": [...]} as defined by GENERATE_TASKS_TOOL's schema."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        tools=[GENERATE_TASKS_TOOL],
        tool_choice={"type": "tool", "name": "submit_generated_tasks"},
        messages=[{"role": "user", "content": raw_text}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_generated_tasks":
            return block.input

    raise ValueError("Claude did not return the expected tool call")


BACKFILL_TASKS_TOOL = {
    "name": "submit_backfill_tasks",
    "description": "Submit exercises for the specific (word, missing type) combinations requested.",
    "input_schema": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "word_kazakh": {
                            "type": "string",
                            "description": "Must exactly match one of the given Kazakh words.",
                        },
                        "type": {"type": "string", "enum": TASK_TYPE_ENUM},
                        "content": {"type": "object", "description": CONTENT_DESCRIPTION},
                    },
                    "required": ["word_kazakh", "type", "content"],
                },
            },
        },
        "required": ["tasks"],
    },
}

BACKFILL_SYSTEM_PROMPT = """Ты готовишь учебные материалы по казахскому языку для ребёнка, который
изучает его как второй язык. Тебе дают список уже известных пар слов казахский-русский и для
каждого слова — список типов упражнений, которых для него ЕЩЁ НЕТ в системе. Нужно сгенерировать
именно эти недостающие типы (и только их) для каждого слова.

Правила по типам (те же, что и при обычной генерации):
- multiple_choice — три неправильных варианта должны быть похожими по смыслу или форме, не случайными.
- anagram — пропусти, если слово длиннее 8 букв или это словосочетание из нескольких слов.
- listening_test — 3 отвлекающих варианта перевода должны быть правдоподобными.
- true_false — shown_translation должен быть неверным примерно в половине случаев.
- fill_blank — пропускай 1-3 буквы внутри слова, не весь корень.
- fill_in_the_blank / sentence_builder — генерируй ТОЛЬКО если получается короткое естественное
  предложение (для fill_in_the_blank — с словом в правильной падежной/спряжённой форме). Если
  естественное предложение не придумывается — просто не включай этот тип в результат для этого
  слова, не выдумывай неестественные фразы. При явном падеже заполняй grammar_case.

Формат content для каждого типа: """ + CONTENT_DESCRIPTION + """

Вызови инструмент submit_backfill_tasks. Не добавляй типы, которых не просили для конкретного
слова, и не пропускай запрошенные типы без веской причины (кроме случая, когда для
fill_in_the_blank/sentence_builder естественное предложение правда не придумывается)."""


def generate_missing_tasks(word_specs: list[dict]) -> list[dict]:
    """word_specs: [{"kazakh", "russian", "missing_types": [type, ...]}, ...]

    Returns a flat list of {"word_kazakh", "type", "content"} dicts - a subset of what was
    requested, since fill_in_the_blank/sentence_builder may still be skipped when no natural
    sentence fits, same as in the initial generation pass.
    """
    lines = [f"{w['kazakh']} ({w['russian']}) — сгенерируй типы: {', '.join(w['missing_types'])}" for w in word_specs]
    user_message = "Слова и недостающие для них типы упражнений:\n" + "\n".join(lines)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        system=BACKFILL_SYSTEM_PROMPT,
        tools=[BACKFILL_TASKS_TOOL],
        tool_choice={"type": "tool", "name": "submit_backfill_tasks"},
        messages=[{"role": "user", "content": user_message}],
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_backfill_tasks":
            return block.input.get("tasks", [])

    raise ValueError("Claude did not return the expected tool call")
