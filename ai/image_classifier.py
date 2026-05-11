"""
Phase 3 — Two-stage image filtering pipeline.

Stage 1 (this module): a cheap classification call that decides whether the
image is a real ad creative worth analysing in depth, plus produces tags and
ethics flags.

Stage 2 (`ImageAnalyzer.analyze_image`): full creative analysis, runs only
for images that pass the gate.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import httpx
from openai import OpenAI

from db.database import get_database
from db.models import Settings

logger = logging.getLogger(__name__)


PREDEFINED_TAGS = (
    "meme",
    "product_shot",
    "lifestyle",
    "flat_lay",
    "before_after",
    "testimonial",
    "event",
    "behind_scenes",
    "ugc",
    "illustration",
    "collage",
    "minimalist",
)


CLASSIFICATION_PROMPT = """Ты — модератор базы рекламных креативов. Тебе показывают одно изображение из Telegram-канала.
Тебе нужно ТОЧНО его классифицировать (не предвзято в сторону False) и вернуть СТРОГО JSON-объект без пояснений и без markdown.

Ключи и их смысл:
{
  "is_ad_creative": true/false,      // Это полноценный рекламный креатив (баннер/постер/лайфстайл-фото товара/мем-баннер)? Или это контент в общем смысле (новостной кадр, фото из чата, схема, обложка статьи)? Если изображение нарисовано/смонтировано/срежиссировано для рекламы — true.
  "is_text_screenshot": true/false,  // Это скриншот переписки / цитата поста / страница с текстом / новостной заголовок крупным планом? То есть основной контент — ТЕКСТ, читаемый прямо с картинки. Если на изображении доминирует читаемый текст (>40% площади или это явный скриншот мессенджера/документа/твита) — true.
  "is_news_photo": true/false,       // Это репортажное / новостное фото (политика, ЧП, события, пресс-конференции)?
  "is_infographic": true/false,      // Плотная инфографика / диаграмма / схема / таблица?
  "has_brand_logo": true/false,      // Виден логотип бренда?
  "has_overlay_text": true/false,    // На картинку наложен текст (заголовок/слоган/описание поверх изображения)? Даже если занимает <50% — отвечай true, если текст явно наложен поверх.
  "ethics_ok": true/false,           // Без шок-контента, трагедий, жестокости, NSFW?
  "ethics_reason": "",               // Короткая причина если ethics_ok=false, иначе пустая строка
  "image_is_key_factor": true/false, // Визуал — главное в посте (vs подпись/текст под картинкой)?
  "solo_image_appeal": 1-10,         // Насколько картинка сама по себе соберёт вовлечение в соцсетях? 1=вообще никак, 10=виральный потенциал
  "tags": ["tag1","tag2"]            // 0..4 тега из whitelist: meme, product_shot, lifestyle, flat_lay, before_after, testimonial, event, behind_scenes, ugc, illustration, collage, minimalist
}

ВАЖНО:
- НЕ занижай флаги по умолчанию. Если на картинке очевидно есть переписка/цитата/скриншот документа — is_text_screenshot ДОЛЖЕН быть true.
- Если флаги противоречат (например, скриншот переписки, по которому делают рекламу) — выбирай ДОМИНИРУЮЩУЮ интерпретацию.
- is_ad_creative и is_text_screenshot могут быть одновременно true (рекламный пост, оформленный как скриншот).
- Возвращай ТОЛЬКО JSON, без преамбулы и markdown."""


@dataclass
class ClassificationResult:
    is_ad_creative: Optional[bool] = None
    is_text_screenshot: bool = False
    is_news_photo: bool = False
    is_infographic: bool = False
    has_brand_logo: bool = False
    has_overlay_text: bool = False
    ethics_ok: bool = True
    ethics_reason: str = ""
    image_is_key_factor: Optional[bool] = None
    solo_image_appeal: float = 0.0
    tags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "ClassificationResult":
        tags_raw = data.get("tags") or []
        if isinstance(tags_raw, str):
            tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]
        tags = [t for t in tags_raw if t in PREDEFINED_TAGS][:4]
        try:
            appeal = float(data.get("solo_image_appeal") or 0)
        except (TypeError, ValueError):
            appeal = 0.0
        return cls(
            is_ad_creative=_to_bool(data.get("is_ad_creative")),
            is_text_screenshot=bool(data.get("is_text_screenshot")),
            is_news_photo=bool(data.get("is_news_photo")),
            is_infographic=bool(data.get("is_infographic")),
            has_brand_logo=bool(data.get("has_brand_logo")),
            has_overlay_text=bool(data.get("has_overlay_text")),
            ethics_ok=bool(data.get("ethics_ok", True)),
            ethics_reason=str(data.get("ethics_reason") or "")[:500],
            image_is_key_factor=_to_bool(data.get("image_is_key_factor")),
            solo_image_appeal=max(0.0, min(10.0, appeal)),
            tags=tags,
            raw=data,
        )


def _to_bool(v) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "y", "да"}
    return bool(v)


@dataclass
class FilterDecision:
    accepted: bool
    reason: str  # human-readable rejection reason; "" if accepted


class ImageClassifier:
    """Stage-1 lightweight classifier built on top of OpenAI Vision."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        if api_key is None:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "OpenAI API ключ не настроен. "
                "Задайте переменную окружения OPENAI_API_KEY в .env файле."
            )
        self.client = OpenAI(api_key=api_key, http_client=httpx.Client())
        self.model = model

    @staticmethod
    def _get_setting(key: str) -> Optional[str]:
        try:
            db = get_database()
            with db.get_session() as session:
                row = session.query(Settings).filter(Settings.key == key).first()
                return row.value if row else None
        except Exception as exc:
            logger.warning("Failed to read setting %s: %s", key, exc)
            return None

    @staticmethod
    def _read_image_b64(image_path: str) -> tuple[str, str]:
        with open(image_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }.get(ext, "image/jpeg")
        return data, mime

    def classify(self, image_path: str) -> Optional[ClassificationResult]:
        if not os.path.exists(image_path):
            logger.warning("Classifier: file not found: %s", image_path)
            return None
        try:
            data, mime = self._read_image_b64(image_path)
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": CLASSIFICATION_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:{mime};base64,{data}"},
                            }
                        ],
                    },
                ],
                max_tokens=300,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or "{}"
            parsed = _safe_json(content)
            return ClassificationResult.from_dict(parsed)
        except Exception as exc:
            logger.error("Classification failed for %s: %s", image_path, exc)
            return None


def _safe_json(content: str) -> dict:
    content = content.strip()
    if content.startswith("```"):
        # strip optional fences
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    try:
        return json.loads(content) or {}
    except json.JSONDecodeError:
        # Very defensive: try to find the first {...} block
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(content[start : end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("Classifier returned non-JSON: %s", content[:200])
        return {}


@dataclass
class FilterConfig:
    enabled: bool = True
    min_solo_appeal: float = 4.0
    reject_text_screenshots: bool = True
    reject_news_photos: bool = True
    reject_unethical: bool = True

    @classmethod
    def from_settings(cls) -> "FilterConfig":
        get = ImageClassifier._get_setting
        return cls(
            enabled=(get("ai_filter_enabled") or "true").lower() == "true",
            min_solo_appeal=_safe_float(get("ai_min_solo_appeal"), 4.0),
            reject_text_screenshots=(get("ai_reject_text_screenshots") or "true").lower() == "true",
            reject_news_photos=(get("ai_reject_news_photos") or "true").lower() == "true",
            reject_unethical=(get("ai_reject_unethical") or "true").lower() == "true",
        )


def _safe_float(v: Optional[str], default: float) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def evaluate(result: ClassificationResult, cfg: FilterConfig) -> FilterDecision:
    """Apply rejection rules from the plan. Returns a FilterDecision."""
    if not cfg.enabled:
        return FilterDecision(True, "")
    if result.is_ad_creative is False:
        return FilterDecision(False, "is_ad_creative=false")
    if cfg.reject_text_screenshots and result.is_text_screenshot:
        return FilterDecision(False, "is_text_screenshot=true")
    if cfg.reject_news_photos and result.is_news_photo:
        return FilterDecision(False, "is_news_photo=true")
    if result.has_overlay_text and result.image_is_key_factor is False:
        return FilterDecision(False, "overlay_text dominates and image is not the key factor")
    if cfg.reject_unethical and not result.ethics_ok:
        return FilterDecision(False, f"ethics: {result.ethics_reason or 'unethical'}")
    if result.solo_image_appeal and result.solo_image_appeal < cfg.min_solo_appeal:
        return FilterDecision(
            False,
            f"solo_image_appeal={result.solo_image_appeal:.1f} < {cfg.min_solo_appeal}",
        )
    return FilterDecision(True, "")
