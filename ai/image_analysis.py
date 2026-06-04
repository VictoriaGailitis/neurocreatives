"""
Image analysis pipeline.

This module orchestrates the two-stage Phase 3 flow:
  1. ImageClassifier — fast gate (ad-creative? ethics? tags?)
  2. ImageAnalyzer.analyze_image — full creative analysis (only on accepted)

It also implements Phase 4 — generating a "target creative" textual prompt
suitable for downstream image-generation services (Midjourney/DALL·E/SD).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

import httpx
from openai import OpenAI

from ai.image_classifier import (
    ClassificationResult,
    FilterConfig,
    ImageClassifier,
    evaluate,
)
from ai.image_similarity import collapse_into_leader, find_duplicate_group_leader
from ai.prefilter import PrefilterConfig
from db.database import get_database
from db.models import Analysis, Image, Post, Settings

logger = logging.getLogger(__name__)


TARGET_CREATIVE_PROMPT = """Based on this advertising image, write a SINGLE detailed paragraph
that can be used as a prompt for an AI image generation service (Midjourney / DALL·E / Stable Diffusion).

Cover:
1. Scene / setting — where it happens.
2. Plot / narrative — what is happening.
3. Characters — who is in the image, their poses and expressions.
4. Composition — arrangement of elements.
5. Lighting and color palette.
6. Style / artistic direction (photography style, illustration etc.).
7. Mood / atmosphere.

DO NOT include:
- Specific brand names or logos.
- Specific text content visible on the image.
- Identities of real people.

Return only the paragraph, in Russian, no preamble, no quotes, no markdown."""


class ImageAnalyzer:
    """Analyser of advertising creatives via OpenAI Vision."""

    def __init__(self, api_key: str = None):
        if api_key is None:
            api_key = os.getenv("OPENAI_API_KEY", "").strip()
            if not api_key:
                raise ValueError(
                    "OpenAI API ключ не настроен. "
                    "Задайте переменную окружения OPENAI_API_KEY в .env файле."
                )

        try:
            http_client = httpx.Client()
            self.client = OpenAI(api_key=api_key, http_client=http_client)
        except Exception as exc:
            logger.error("OpenAI client init failed: %s", exc)
            raise

        self.api_key = api_key
        self.model = "gpt-4o-mini"

        custom_prompt = self._get_setting("analysis_prompt")
        self.system_prompt = f"""You are analyzing an advertising creative.

{custom_prompt if custom_prompt else 'Что на этом фото?'}

Return JSON with:
- type: тип креатива (баннер, сторис, пост и т.д.)
- scene: описание сцены (кратко)
- objects: список объектов на изображении
- emotion: доминирующая эмоция
- category: категория рекламы (продукт, услуга, бренд и т.д.)
- text_present: есть ли текст на изображении (да/нет)
- visual_strength_score: оценка визуальной силы от 1 до 10

Be concise. Answer in Russian."""

    # ---------- helpers ----------

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

    # ---------- core analysis ----------

    def analyze_image(self, image_path: str) -> Optional[Dict]:
        """Stage 2 — full creative analysis."""
        try:
            if not os.path.exists(image_path):
                logger.warning("File not found: %s", image_path)
                return None

            data, mime = self._read_image_b64(image_path)

            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
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
                max_tokens=500,
                temperature=0.3,
            )

            content = response.choices[0].message.content or ""
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            return json.loads(content)

        except json.JSONDecodeError as exc:
            logger.error("Analyzer JSON parse failed: %s", exc)
            return None
        except Exception as exc:
            logger.error("Analyzer error for %s: %s", image_path, exc)
            return None

    # ---------- Phase 4: target creative description ----------

    @staticmethod
    def _is_target_candidate(
        cls_result: Optional[ClassificationResult],
        full_result: Optional[Dict],
    ) -> bool:
        """Решает, подходит ли изображение на роль «целевого креатива».

        Целевой креатив — это пост, для которого мы хотим получить готовое
        описание-промпт под генерацию похожей картинки. Поэтому требуем:

          * это рекламный креатив (``is_ad_creative=True``);
          * он этичный (``ethics_ok=True``);
          * картинка является ключевым фактором поста, а не просто
            «декором» к тексту (``image_is_key_factor`` не False);
          * не текстовый скриншот / не репортажное фото / не плотная
            инфографика — таких в целевую генерацию мы не хотим;
          * визуальная сила и solo-appeal достаточно высокие
            (приоритет на то, что хорошо «выстреливает» и в одиночку).

        Если классификатор по какой-то причине не отработал, мы делаем
        fallback на эвристику: считаем подходящим всё, что прошло первый
        этап и имеет visual_strength_score >= 6 в полном анализе.
        """
        if cls_result is None:
            try:
                vs = float((full_result or {}).get("visual_strength_score") or 0)
            except (TypeError, ValueError):
                vs = 0.0
            return vs >= 6.0

        if cls_result.is_ad_creative is False:
            return False
        if not cls_result.ethics_ok:
            return False
        if cls_result.is_text_screenshot or cls_result.is_news_photo:
            return False
        if cls_result.is_infographic:
            # Инфографика — это график/таблица, а не сцена для генерации.
            return False
        if cls_result.image_is_key_factor is False:
            return False
        if cls_result.solo_image_appeal and cls_result.solo_image_appeal < 5.0:
            return False
        return True

    def generate_target_description(self, image_path: str) -> Optional[str]:
        """Generate a generation-ready prompt describing the scene."""
        try:
            if not os.path.exists(image_path):
                logger.warning("File not found: %s", image_path)
                return None
            data, mime = self._read_image_b64(image_path)
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": TARGET_CREATIVE_PROMPT},
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
                max_tokens=600,
                temperature=0.5,
            )
            text = (response.choices[0].message.content or "").strip()
            return text or None
        except Exception as exc:
            logger.error("Target-description generation failed: %s", exc)
            return None

    # ---------- batch driver ----------

    def analyze_all_unanalyzed(self, return_metrics: bool = False):
        """Единый оркестратор пайплайна L1 → L2 → L3 по всем не разобранным фото.

        Стадии:
          * **L1** (phash-дедуп) — near-duplicate сворачивается в самый
            популярный пост группы; такие фото НЕ анализируются (экономия AI).
          * **L2** (обязательный AI-гейт ``ImageClassifier`` + ``evaluate``) —
            отсев нерелевантных креативов (скриншоты текста, новости, этика…).
          * **L3** (полный анализ ``analyze_image``) — только для прошедших L2.

        Возвращает ``int`` (число прошедших полный анализ) для обратной
        совместимости, либо dict-метрики воронки при ``return_metrics=True``.
        """
        db = get_database()

        # Build the classifier once (shares api_key)
        try:
            classifier = ImageClassifier(api_key=self.api_key, model=self.model)
        except Exception as exc:
            logger.warning("Classifier disabled, running full analysis only: %s", exc)
            classifier = None
        filter_cfg = FilterConfig.from_settings()
        prefilter_cfg = PrefilterConfig.from_settings()

        metrics = {"rejected_l1": 0, "rejected_l2": 0, "passed_full": 0, "total": 0}

        with db.get_session() as session:
            # Берём только фото без анализа, чей пост ещё не отсеян на L0/L1.
            images = (
                session.query(Image)
                .outerjoin(Analysis)
                .outerjoin(Post, Image.post_id == Post.id)
                .filter(Analysis.id == None)  # noqa: E711
                .filter(
                    (Post.id == None)  # noqa: E711
                    | (Post.prefilter_status.notin_(["duplicate", "rejected"]))
                )
                .all()
            )

            total = len(images)
            metrics["total"] = total
            if total == 0:
                print("✓ Все изображения уже проанализированы")
                return metrics if return_metrics else 0

            print(f"🔍 Найдено {total} изображений для анализа")
            analyzed = 0
            rejected = 0

            for idx, image in enumerate(images, 1):
                print(f"\n[{idx}/{total}] Анализ изображения ID: {image.id}")
                print(f"  Файл: {image.file_path}")

                post = image.post

                # ---- L1: phash-дедуп (сворачивание в лидера группы) ----
                if (
                    prefilter_cfg.dedupe_enabled
                    and post is not None
                    and post.image_hash
                ):
                    leader = find_duplicate_group_leader(
                        session,
                        post.image_hash,
                        exclude_post_id=post.id,
                        threshold=prefilter_cfg.dedupe_threshold,
                    )
                    if leader is not None:
                        actual_leader = collapse_into_leader(session, post, leader)
                        if actual_leader.id != post.id:
                            # Текущий пост стал дублем — анализ не нужен.
                            metrics["rejected_l1"] += 1
                            session.commit()
                            print(
                                f"  🧬 L1 дубль → свёрнут в пост #{actual_leader.id}"
                            )
                            continue
                        # Иначе пост — новый лидер группы, продолжаем к L2/L3.

                # ---- L2: обязательный AI-гейт классификатора ----
                cls_result: Optional[ClassificationResult] = None
                if classifier is not None:
                    cls_result = classifier.classify(image.file_path)

                decision = (
                    evaluate(cls_result, filter_cfg)
                    if cls_result
                    else None
                )

                full_result = None
                target_description = None
                if decision is None or decision.accepted:
                    # ---- L3: полный анализ креатива ----
                    full_result = self.analyze_image(image.file_path)
                    # Phase 4: target description генерируется только если изображение
                    # действительно подходит на роль «целевого креатива».
                    # Это экономит OpenAI-кредиты и убирает мусорные описания.
                    if full_result and self._is_target_candidate(cls_result, full_result):
                        target_description = self.generate_target_description(
                            image.file_path
                        )
                    elif full_result:
                        print("  ⏭ Полный анализ сохранён, но target description "
                              "не генерируется (не подходит как целевой креатив)")
                else:
                    rejected += 1
                    metrics["rejected_l2"] += 1
                    print(f"  🚫 L2 отклонено фильтром: {decision.reason}")

                # Persist analysis row regardless — store classification + filter decision
                self._save_analysis(
                    session=session,
                    image_id=image.id,
                    full_result=full_result,
                    cls_result=cls_result,
                    rejected_reason=decision.reason if decision and not decision.accepted else "",
                    target_description=target_description,
                )

                # Отражаем итог стадий L2/L3 в статусе предфильтра поста.
                if post is not None:
                    if decision is not None and not decision.accepted:
                        post.prefilter_status = "rejected"
                        post.prefilter_stage = "L2"
                        post.prefilter_reason = (decision.reason or "")[:500]
                    else:
                        post.prefilter_status = "passed"
                        post.prefilter_stage = "L3"

                session.commit()

                if full_result is not None:
                    analyzed += 1
                    metrics["passed_full"] += 1
                    print("  ✓ Анализ сохранён")

                if idx < total:
                    time.sleep(0.6)

            print(
                f"\n✓ Анализ завершён: passed_full={analyzed}, "
                f"rejected_l1={metrics['rejected_l1']}, "
                f"rejected_l2={rejected}, total={total}"
            )
            return metrics if return_metrics else analyzed

    # ---------- persistence ----------

    @staticmethod
    def _save_analysis(
        session,
        *,
        image_id: int,
        full_result: Optional[Dict],
        cls_result: Optional[ClassificationResult],
        rejected_reason: str,
        target_description: Optional[str],
    ) -> None:
        analysis = Analysis(image_id=image_id)

        if full_result:
            analysis.scene = full_result.get("scene", "")
            analysis.objects = (
                ", ".join(full_result["objects"])
                if isinstance(full_result.get("objects"), list)
                else (full_result.get("objects") or "")
            )
            analysis.emotion = full_result.get("emotion", "")
            analysis.creative_type = full_result.get("type", "")
            analysis.text_present = str(full_result.get("text_present", ""))
            try:
                analysis.visual_strength_score = float(
                    full_result.get("visual_strength_score") or 0
                )
            except (TypeError, ValueError):
                analysis.visual_strength_score = 0.0

        if cls_result:
            analysis.is_ad_creative = cls_result.is_ad_creative
            analysis.is_text_screenshot = cls_result.is_text_screenshot
            analysis.is_news_photo = cls_result.is_news_photo
            analysis.is_infographic = cls_result.is_infographic
            analysis.has_brand_logo = cls_result.has_brand_logo
            analysis.has_overlay_text = cls_result.has_overlay_text
            analysis.ethics_flag = not cls_result.ethics_ok
            analysis.ethics_reason = cls_result.ethics_reason
            analysis.image_is_key_factor = cls_result.image_is_key_factor
            analysis.solo_image_appeal = cls_result.solo_image_appeal
            analysis.tags = json.dumps(cls_result.tags, ensure_ascii=False)

        if target_description:
            analysis.target_creative_description = target_description
            analysis.target_creative_ready = True

        if rejected_reason:
            analysis.moderation_status = "rejected"
            analysis.moderation_comment = rejected_reason
            analysis.moderated_at = datetime.utcnow()

        session.add(analysis)
