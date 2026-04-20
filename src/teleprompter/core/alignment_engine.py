"""語音對齊引擎：句子定位 + 字元級對齊（卡拉 OK 推進）。

設計重點：
- 兩層對齊：先用滑動窗口模糊比對找出當前句，再在句內做字元級對齊。
- **拼音比對**：同時用字元與拼音計算相似度，解決繁/簡/同音字/Whisper 同音誤辨問題
  （例：「實作」與「十座」字元完全不同但拼音相同）。
- **滑動文字緩衝**：累積最近 N 字的辨識文字作為比對輸入，短 delta 也能穩定匹配。
- **卡住偵測**：連續多次匹配失敗時擴大搜尋範圍，避免整段卡死。
- 防抖動：高相似度立即更新；中相似度需連續確認；低相似度忽略。
- 方向偏好：除非分數壓倒性，否則只允許前進或極小幅度回退（重複念）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Optional

from .text_utils import (
    combined_ratio,
    pinyin_tokens_with_positions,
    to_pinyin_form,
)
from .transcript_loader import Sentence, Transcript, normalize_text


# === 預設參數（可在 AlignmentEngine 建構時覆寫） ===
DEFAULT_WINDOW_AHEAD = 8         # 往前看幾句
DEFAULT_WINDOW_BACK = 3          # 往後看幾句（容忍重複）
DEFAULT_JUMP_WINDOW = 16         # 跳句容忍時的擴大窗口
HIGH_CONFIDENCE = 70             # 立即更新的相似度
MID_CONFIDENCE = 60              # 需連續確認（與 HIGH 差 10 分，避免中間模糊帶太廣）
MID_CONFIRM_COUNT = 2            # MID 需連續確認次數
MID_MIN_BUFFER_LEN = 3           # MID 路徑要求的最短緩衝長度
MIN_RECOGNIZED_LEN = 1           # 串流可一次只進來 1~2 字
MAX_BACKWARD_GLOBAL_CHARS = 4    # 一般情況下允許的最大回退字數（防抖）
RECENT_BUFFER_CHARS = 60         # 累積最近多少字做比對（足夠容納單句完整匹配 + 跨句上下文）
STUCK_THRESHOLD = 4              # 連續幾次低信心後進入「卡住」模式擴大搜尋
GLOBAL_SEARCH_THRESHOLD = 12     # 連續幾次仍卡住後做全域搜尋（調高避免誤跳）
GLOBAL_SEARCH_MARGIN = 15        # 全域搜尋結果要比原本高多少分才採用

# 歧義判定（軸 2 改寫）：改用「Top-2 分數差」而非絕對門檻
# 即使兩個候選都 raw 100，只要彼此差距 ≥ AMBIGUITY_GAP 就不算歧義（讓近的贏）
AMBIGUITY_MIN_SCORE = 80         # 候選分數至少 ≥ 80 才有資格進入歧義判定
AMBIGUITY_GAP = 8                # Top-1 與 Top-2 距離 ≥ AMBIGUITY_DIST 的候選分差 < 此值才算歧義
AMBIGUITY_DIST = 3               # 歧義判定的最小距離（句數）

# 大跳段閘門：跳超過幾句必須達到的最低信心與最短辨識長度
BIG_JUMP_SENT_DIST = 3           # 超過幾句算大跳段
BIG_JUMP_MIN_SCORE = 78          # 大跳段最低分數要求
BIG_JUMP_MIN_REC_LEN = 5         # 大跳段最短辨識字數（避免 1-2 字就亂跳）

# 漏講偵測（軸 1）：每句最大進度低於此門檻 → 視為被漏講
SKIPPED_PROGRESS_THRESHOLD = 0.7

# 信心加權進度上限：低信心 commit 不能把進度推到 1.0，避免 hallucination 誇大
PROGRESS_CAP_HIGH = 1.0           # score ≥ HIGH_PROGRESS_BAR (75) 時的進度上限
PROGRESS_CAP_MID = 0.6            # 60-74 時的進度上限
PROGRESS_CAP_LOW = 0.3            # < 60 時的進度上限
HIGH_PROGRESS_BAR = 75            # 「真實高信心」的標準
HIGH_CONFIDENCE_PROGRESS_FLOOR = 0.5  # 該句須累積 ≥ 此 high-progress 才算「真的念過」

# 時間感知卡住自救
# 常數順序原則：嚴格 ≥ 寬鬆，避免邏輯倒序
# 一般 HIGH (70) > BOUNDARY (60) > 軟卡 (55) > 硬卡 (48)
# 一般 MID (55) > 卡住 MID (42)
STUCK_TIME_SOFT_S = 0.8           # 卡住此秒數後，降低閾值嘗試 commit（更靈敏）
STUCK_TIME_HARD_S = 2.0           # 卡住此秒數後，更激進的閾值
STUCK_SOFT_HIGH_CONFIDENCE = 55   # 軟卡住 HIGH 閾值（< 一般 HIGH=70）
STUCK_HARD_HIGH_CONFIDENCE = 48   # 硬卡住 HIGH 閾值
STUCK_SOFT_MID_CONFIDENCE = 42    # 軟卡住 MID 閾值
BOUNDARY_HIGH_CONFIDENCE = 60     # 句末標點 HIGH 閾值（< 一般 HIGH=70 但 > MID=55）

# 邊界標點（含中英）
BOUNDARY_PUNCT = "。！？!?；;,，"

# 跳進距離上限：避免單次更新跳太遠（解決「字幕跑到下一段去」）
MAX_FORWARD_CHARS_PER_COMMIT = 40  # 一般 commit 上限
MAX_FORWARD_CHARS_HIGH = 80        # 高信心 commit 上限（≥85 分）

# 語速估算與時間軸軟推進
DEFAULT_READING_SPEED = 4.5        # 預設 chars/sec（中文一般語速）
SPEED_HISTORY_LEN = 10             # 保留最近幾次 commit 估算速度
SOFT_ADVANCE_MIN_GAP_S = 1.2       # 距離上次 commit 至少這麼久才考慮軟推進
SOFT_ADVANCE_MAX_CHARS = 4         # 每次軟推進最多前進的字元數（保守）


@dataclass
class AlignmentResult:
    """對齊結果。

    skipped_ranges 改為列表，可一次回傳多個不連續的漏講區段。
    保留 skipped_start/skipped_end 屬性供舊測試/外部程式碼相容
    （取列表中所有區段的最小 start 與最大 end 作為 bounding box）。
    """

    global_char_pos: int        # 全文中的目前字元位置（高亮推進到的位置）
    sentence_index: int         # 目前句索引
    confidence: float           # 0-100 相似度
    updated: bool               # 是否更新了位置
    reason: str = ""            # 除錯訊息
    skipped_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def has_skipped(self) -> bool:
        return bool(self.skipped_ranges)

    @property
    def skipped_start(self) -> int:
        """相容性：所有區段的最小起點（無區段時 -1）。"""
        return min((s for s, _ in self.skipped_ranges), default=-1)

    @property
    def skipped_end(self) -> int:
        """相容性：所有區段的最大終點（無區段時 -1）。"""
        return max((e for _, e in self.skipped_ranges), default=-1)


class AlignmentEngine:
    """維護目前提詞位置，並依新到的辨識文字推進高亮。"""

    def __init__(
        self,
        transcript: Optional[Transcript] = None,
        *,
        window_ahead: int = DEFAULT_WINDOW_AHEAD,
        window_back: int = DEFAULT_WINDOW_BACK,
        jump_window: int = DEFAULT_JUMP_WINDOW,
    ) -> None:
        self.transcript: Optional[Transcript] = None
        self.sentences: list[Sentence] = []
        # 每句的拼音快取（與 sentences 同長度）
        self._sentence_pinyin: list[str] = []
        # 每句的 pinyin token + 對應 normalized 結束位置，用於拼音級字元對齊
        self._sentence_pinyin_tokens: list[list[tuple[str, int]]] = []
        self.window_ahead = window_ahead
        self.window_back = window_back
        self.jump_window = jump_window

        self.current_sentence_index: int = 0
        self.current_global_char: int = 0
        self._mid_pending: dict[int, int] = {}  # sentence_idx -> 連續中等信心命中次數
        self._stagnant_ticks: int = 0            # 連續多少次都沒有顯著匹配
        self._recent_buffer: str = ""            # 滑動最近辨識文字緩衝
        # 軸 1：每句最大進度 0~1（單調遞增），用於漸進漂移時也能偵測漏講
        self._sentence_max_progress: dict[int, float] = {}
        # 防護層：每句的「高信心進度」（只在 HIGH 信心 commit 時更新）
        # 用於辨別「真實念過」vs「靠低信心匹配湊出進度」
        self._sentence_high_confidence_progress: dict[int, float] = {}
        # 逐字標記：每句已匹配的 normalized 字元索引集合
        # 用於「句內中段缺字也標紅」的精細漏講偵測
        self._sentence_matched_chars: dict[int, set[int]] = {}
        # 拼音層匹配的字元集合（Whisper 同音誤辨保護）
        # 例：「實作」(script) vs「十座」(Whisper) → 字元不匹配但拼音對應 → 算作「念過」
        self._sentence_phoneme_matched_chars: dict[int, set[int]] = {}
        # 即時可調的閾值（apply_stability_mode 控制），預設 balanced
        self.apply_stability_mode("balanced")
        # 最大跳段限制（0 = 不限制）
        self._max_forward_sentences: int = 0
        self._max_forward_chars: int = 0
        # 時間感知：追蹤上次成功 commit / 上次任何 update 的時間
        self._last_commit_time: float = time.monotonic()
        self._last_update_time: float = time.monotonic()
        # 語速估算：保留最近 N 次 commit 的 (時間, 累積字元) 用於計算 chars/sec
        self._commit_history: list[tuple[float, int]] = []
        if transcript is not None:
            self.set_transcript(transcript)

    # ---------- 公開 API ----------

    def set_transcript(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.sentences = transcript.sentences
        self._sentence_pinyin = [to_pinyin_form(s.normalized) for s in self.sentences]
        self._sentence_pinyin_tokens = [
            pinyin_tokens_with_positions(s.normalized) for s in self.sentences
        ]
        self.reset()

    def reset(self) -> None:
        self.current_sentence_index = 0
        self.current_global_char = self.sentences[0].start if self.sentences else 0
        self._mid_pending.clear()
        self._stagnant_ticks = 0
        self._recent_buffer = ""
        self._sentence_max_progress.clear()
        self._sentence_high_confidence_progress.clear()
        self._sentence_matched_chars.clear()
        self._sentence_phoneme_matched_chars.clear()
        self._last_commit_time = time.monotonic()
        self._last_update_time = time.monotonic()
        self._commit_history.clear()

    def jump_to_sentence(self, sentence_index: int) -> AlignmentResult:
        """手動跳到指定句（使用者按上下鍵或點擊）。手動跳不標漏講。"""
        if not self.sentences:
            return AlignmentResult(0, 0, 0.0, False, "no transcript")
        sentence_index = max(0, min(sentence_index, len(self.sentences) - 1))
        sent = self.sentences[sentence_index]
        self.current_sentence_index = sentence_index
        self.current_global_char = sent.start
        self._mid_pending.clear()
        self._stagnant_ticks = 0
        self._recent_buffer = ""
        # 把已跳到的句子標為「已念」進度（避免後續被誤判漏講）
        self._sentence_max_progress[sentence_index] = 0.0
        return AlignmentResult(
            sent.start, sentence_index, 100.0, True, "manual jump"
        )

    def jump_to_global_char(self, global_char: int) -> AlignmentResult:
        """手動跳到指定全文字元位置（滑鼠點擊任意字）。"""
        if not self.sentences:
            return AlignmentResult(0, 0, 0.0, False, "no transcript")
        global_char = max(0, min(global_char, self.transcript.total_chars))
        sentence_index = self.current_sentence_index
        for i, s in enumerate(self.sentences):
            if s.start <= global_char < s.end:
                sentence_index = i
                break
        else:
            sentence_index = len(self.sentences) - 1

        self.current_sentence_index = sentence_index
        self.current_global_char = global_char
        self._mid_pending.clear()
        self._stagnant_ticks = 0
        self._recent_buffer = ""
        return AlignmentResult(
            global_char, sentence_index, 100.0, True, "manual jump (char)"
        )

    def update(self, recognized_text: str) -> AlignmentResult:
        """主要入口：包裝 _update_inner 提供異常保護，避免任何 bug 讓應用崩潰。"""
        try:
            return self._update_inner(recognized_text)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("alignment update failed; preserving state")
            return AlignmentResult(
                self.current_global_char,
                self.current_sentence_index,
                0.0,
                False,
                "internal error (state preserved)",
            )

    def _update_inner(self, recognized_text: str) -> AlignmentResult:
        if not self.sentences:
            return AlignmentResult(0, 0, 0.0, False, "no transcript")

        now = time.monotonic()
        self._last_update_time = now
        # 此次 update 是否含「句末標點」訊號 → 表示使用者剛念完一句，更寬容
        is_boundary_text = any(ch in BOUNDARY_PUNCT for ch in recognized_text)
        # 卡住程度（基於上次 commit 經過時間）
        time_since_commit = now - self._last_commit_time
        soft_stuck = time_since_commit >= STUCK_TIME_SOFT_S
        hard_stuck = time_since_commit >= STUCK_TIME_HARD_S

        norm = normalize_text(recognized_text)
        if len(norm) < MIN_RECOGNIZED_LEN:
            return AlignmentResult(
                self.current_global_char,
                self.current_sentence_index,
                0.0,
                False,
                "recognized text too short",
            )

        # 滑動緩衝
        self._recent_buffer = (self._recent_buffer + norm)[-RECENT_BUFFER_CHARS:]
        search_text = self._recent_buffer
        recognized_pinyin = to_pinyin_form(search_text)

        # 1) 視窗內定位（含 proximity bonus / 大跳閘門）
        best_idx, best_score = self._locate_sentence(search_text)

        # 2) 視窗外前瞻掃描（找跳段候選，僅在文字夠長時）
        forward_cand = None
        if len(search_text) >= BIG_JUMP_MIN_REC_LEN:
            forward_cand = self._find_forward_skip_candidate(search_text)

        # 2.5) 全域歧義偵測：合併「視窗最佳」+「前瞻候選」+ 整份其他高分候選
        # 若同時有多個 raw ≥ AMBIGUITY_SCORE 且彼此距離 ≥ AMBIGUITY_DIST → 歧義
        # 直接 return 不更新（連 forward override 也不允許）
        if self._is_globally_ambiguous(search_text, recognized_pinyin):
            self._stagnant_ticks += 1
            return AlignmentResult(
                self.current_global_char,
                self.current_sentence_index,
                MID_CONFIDENCE - 1.0,
                False,
                "globally ambiguous",
            )

        # 沒有歧義：可採用 forward 候選
        if forward_cand is not None:
            c_idx, c_score = forward_cand
            if c_score >= BIG_JUMP_MIN_SCORE and c_score > best_score + 5:
                best_idx, best_score = c_idx, c_score

        # 3) 卡住 → 擴大窗口
        if best_score < self._high_confidence and self._stagnant_ticks >= STUCK_THRESHOLD:
            jump_idx, jump_score = self._locate_sentence(search_text, expand=True)
            if jump_score > best_score:
                best_idx, best_score = jump_idx, jump_score

        # 4) 持續卡住 → 全域搜尋
        if (
            best_score < self._high_confidence
            and self._stagnant_ticks >= GLOBAL_SEARCH_THRESHOLD
            and len(search_text) >= BIG_JUMP_MIN_REC_LEN
        ):
            g_idx, g_score = self._global_locate(search_text)
            if g_score >= BIG_JUMP_MIN_SCORE and g_score > best_score + GLOBAL_SEARCH_MARGIN:
                best_idx, best_score = g_idx, g_score

        # 4) 動態閾值：卡住越久、有句末標點時，閾值降低，讓低信心也能 commit
        high_threshold = self._high_confidence
        mid_threshold = self._mid_confidence
        if hard_stuck:
            high_threshold = self._stuck_hard_high
            mid_threshold = self._stuck_soft_mid
        elif soft_stuck or is_boundary_text:
            high_threshold = self._stuck_soft_high if soft_stuck else self._boundary_high
            mid_threshold = self._stuck_soft_mid

        # 5) HIGH 路徑
        if best_score >= high_threshold:
            self._mid_pending.clear()
            new_pos, consumed = self._char_align(best_idx, search_text, best_score)
            self._trim_recent_buffer(consumed)
            reason = "high confidence"
            if hard_stuck:
                reason = "stuck-recovery (hard)"
            elif soft_stuck:
                reason = "stuck-recovery (soft)"
            elif is_boundary_text:
                reason = "boundary punctuation"
            return self._commit(best_idx, new_pos, best_score, reason)

        # 6) MID 路徑
        if best_score >= mid_threshold:
            require_confirm = not (soft_stuck or hard_stuck or is_boundary_text)
            min_buf_ok = (
                len(search_text) >= self._mid_min_buffer
                or soft_stuck
                or hard_stuck
                or is_boundary_text
            )
            if min_buf_ok and not require_confirm:
                self._mid_pending.clear()
                new_pos, consumed = self._char_align(best_idx, search_text, best_score)
                self._trim_recent_buffer(consumed)
                return self._commit(best_idx, new_pos, best_score, "mid (relaxed)")
            if min_buf_ok:
                count = self._mid_pending.get(best_idx, 0) + 1
                self._mid_pending[best_idx] = count
                if count >= self._mid_confirm_count:
                    self._mid_pending.clear()
                    new_pos, consumed = self._char_align(best_idx, search_text, best_score)
                    self._trim_recent_buffer(consumed)
                    return self._commit(best_idx, new_pos, best_score, "mid confirmed")
            self._stagnant_ticks += 1
            return AlignmentResult(
                self.current_global_char,
                self.current_sentence_index,
                best_score,
                False,
                "mid pending",
            )

        # 7) 低信心：忽略
        self._stagnant_ticks += 1
        return AlignmentResult(
            self.current_global_char,
            self.current_sentence_index,
            best_score,
            False,
            "low confidence ignored",
        )

    # ---------- 內部 ----------

    def _commit(
        self, sent_idx: int, new_global_pos: int, score: float, reason: str
    ) -> AlignmentResult:
        # 方向偏好：往後（回退）需要更高的信心才允許
        is_backward = (
            sent_idx < self.current_sentence_index
            or new_global_pos < self.current_global_char
        )
        if is_backward and score < 90:
            return AlignmentResult(
                global_char_pos=self.current_global_char,
                sentence_index=self.current_sentence_index,
                confidence=score,
                updated=False,
                reason=reason + " (backward rejected)",
            )
        # 超大幅回退 → 壓抑
        if new_global_pos < self.current_global_char - MAX_BACKWARD_GLOBAL_CHARS:
            new_global_pos = self.current_global_char

        # 跳進距離上限（防超前）：只在中低信心 (< 80) 時套用，避免擋掉正當大跳段
        # 高信心代表系統很確定，使用者真的跳到那個位置 → 不限制
        forward_jump = new_global_pos - self.current_global_char
        if forward_jump > MAX_FORWARD_CHARS_PER_COMMIT and score < 80:
            new_global_pos = self.current_global_char + MAX_FORWARD_CHARS_PER_COMMIT
            for i, s in enumerate(self.sentences):
                if s.start <= new_global_pos < s.end:
                    sent_idx = i
                    break

        prev_sent_idx = self.current_sentence_index
        prev_pos = self.current_global_char

        # 1) 在離開 prev_sent_idx 之前，先把 prev_pos 在該句的進度紀錄起來
        # 使用本次 commit 的 score，避免低信心 commit 把 prev sent 進度誇大
        if 0 <= prev_sent_idx < len(self.sentences):
            self._record_progress(prev_sent_idx, prev_pos, score)

        # 2) 漏講偵測：往前推進時回頭檢查每個經過的句子
        skipped_ranges: list[tuple[int, int]] = []
        is_normal_advance = (sent_idx == prev_sent_idx + 1)
        if sent_idx > prev_sent_idx:
            for i in range(prev_sent_idx, sent_idx):
                this_normal_advance = is_normal_advance and (i == prev_sent_idx)
                ranges = self._compute_skipped_range_for_sentence(i, this_normal_advance)
                skipped_ranges.extend(ranges)

        # 2.5) 檢查當前句內的漏講
        # 跨句推進時（sent_idx > prev）：檢查新句 cursor 之前的未匹配字元
        # 同句超大跳進（sent_idx == prev 但前進 ≥ 10 字）：使用者明顯跳過一大段 → 即時標紅
        if sent_idx >= prev_sent_idx and sent_idx < len(self.sentences):
            forward_in_sent = new_global_pos - prev_pos
            should_check = (
                sent_idx > prev_sent_idx
                or forward_in_sent >= 10
            )
            if should_check:
                # 同句檢查時，只標「cursor 前 3 字」以外的未匹配區段
                # （cursor 附近的字可能還在被辨識中，避免過度即時標紅）
                check_up_to = new_global_pos - 3 if sent_idx == prev_sent_idx else new_global_pos
                current_sent_skips = self._compute_skipped_range_for_sentence(
                    sent_idx, is_normal_advance=False, up_to_global_pos=check_up_to
                )
                skipped_ranges.extend(current_sent_skips)

        # 3) 更新狀態
        self.current_sentence_index = sent_idx
        self.current_global_char = max(self.current_global_char, new_global_pos)
        # 紀錄新句的進度（傳入 score 讓低信心 commit 不會把進度推到 1.0）
        self._record_progress(sent_idx, self.current_global_char, score)
        self._stagnant_ticks = 0
        now = time.monotonic()
        self._last_commit_time = now
        # 紀錄語速歷史
        self._commit_history.append((now, self.current_global_char))
        if len(self._commit_history) > SPEED_HISTORY_LEN:
            self._commit_history.pop(0)

        return AlignmentResult(
            global_char_pos=self.current_global_char,
            sentence_index=sent_idx,
            confidence=score,
            updated=True,
            reason=reason,
            skipped_ranges=skipped_ranges,
        )

    def _record_progress(
        self, sent_idx: int, global_pos: int, score: float = 100.0
    ) -> None:
        """更新某句的最大進度（單調遞增），依 score 決定上限。

        - HIGH 信心 (≥75)：可記錄到 1.0；同時更新「高信心進度」
        - MID (60-74)：上限 0.6
        - LOW (<60)：上限 0.3（避免 hallucination 把進度推到很高）
        """
        if not (0 <= sent_idx < len(self.sentences)):
            return
        sent = self.sentences[sent_idx]
        sent_len = max(1, sent.end - sent.start)
        raw_progress = max(0.0, min(1.0, (global_pos - sent.start) / sent_len))

        # 依 score 決定上限
        if score >= HIGH_PROGRESS_BAR:
            cap = PROGRESS_CAP_HIGH
        elif score >= MID_CONFIDENCE:
            cap = PROGRESS_CAP_MID
        else:
            cap = PROGRESS_CAP_LOW
        capped_progress = min(raw_progress, cap)

        cur = self._sentence_max_progress.get(sent_idx, 0.0)
        if capped_progress > cur:
            self._sentence_max_progress[sent_idx] = capped_progress

        # 同步追蹤高信心進度（只在 HIGH commit 時記錄）
        if score >= HIGH_PROGRESS_BAR:
            cur_hp = self._sentence_high_confidence_progress.get(sent_idx, 0.0)
            if raw_progress > cur_hp:
                self._sentence_high_confidence_progress[sent_idx] = raw_progress

    def _compute_skipped_range_for_sentence(
        self, sent_idx: int, is_normal_advance: bool = False,
        up_to_global_pos: Optional[int] = None,
    ) -> list[tuple[int, int]]:
        """回傳該句中所有「未念」的全文字元範圍（list 因可能含多段）。

        參數 up_to_global_pos：限制只檢查到該全文位置（用於檢查當前句的「cursor 之前」）。
        若為 None，檢查整句。

        核心邏輯：
        - heard = 字元匹配 ∪ 拼音匹配（避免 Whisper 同音誤辨被當漏講）
        - 把 heard 之外連續 ≥ 2 字的 run 標為漏講
        """
        if not (0 <= sent_idx < len(self.sentences)):
            return []
        sent = self.sentences[sent_idx]
        if not sent.normalized:
            return []

        matched = self._sentence_matched_chars.get(sent_idx, set())
        phoneme = self._sentence_phoneme_matched_chars.get(sent_idx, set())
        heard = matched | phoneme  # 字元 OR 拼音 任一命中即視為「念過」

        if not heard:
            return [(sent.start, sent.end)]

        high_progress = self._sentence_high_confidence_progress.get(sent_idx, 0.0)
        max_progress = self._sentence_max_progress.get(sent_idx, 0.0)
        if max_progress >= SKIPPED_PROGRESS_THRESHOLD and high_progress < HIGH_CONFIDENCE_PROGRESS_FLOOR:
            return [(sent.start, sent.end)]

        norm_len = len(sent.normalized)

        # 計算掃描範圍上限（normalized 索引）
        # 若 up_to_global_pos 給定且在句內，只掃描到對應的 normalized 位置
        scan_end = norm_len
        if up_to_global_pos is not None and up_to_global_pos < sent.end:
            # 找出 up_to_global_pos 對應的 normalized 索引
            for i, gp in enumerate(sent.char_map):
                if gp >= up_to_global_pos:
                    scan_end = i
                    break

        runs: list[tuple[int, int]] = []
        in_run = False
        run_start = 0
        for i in range(scan_end):
            if i not in heard:
                if not in_run:
                    in_run = True
                    run_start = i
            else:
                if in_run:
                    runs.append((run_start, i))
                    in_run = False
        if in_run:
            runs.append((run_start, scan_end))

        # 標 ≥ 2 字的缺口（拼音保護後 1-2 字漏字就算真實漏講）
        result: list[tuple[int, int]] = []
        for r_s, r_e in runs:
            if r_e - r_s < 2:
                continue
            global_s = sent.normalized_to_global(r_s)
            global_e = sent.normalized_to_global(min(r_e, norm_len - 1))
            # normalized_to_global 給的是該字元位置，要 +1 包含該字元
            if r_e <= norm_len - 1:
                global_e = sent.normalized_to_global(r_e - 1) + 1
            else:
                global_e = sent.end
            if global_e > global_s:
                result.append((global_s, global_e))
        return result

    # 即時可調的閾值（受 stability_mode 影響），預設取自全域常數
    def __post_init_thresholds(self):
        # 此方法由 apply_stability_mode() 設定，不直接呼叫
        pass

    def set_max_forward_range(
        self, max_sentences: int = 0, max_chars: int = 0
    ) -> None:
        """設定最大跳段範圍。0 表示不限制。"""
        self._max_forward_sentences = max(0, int(max_sentences))
        self._max_forward_chars = max(0, int(max_chars))

    def apply_stability_mode(self, mode: str) -> None:
        """套用穩定性模式：conservative / balanced / aggressive。

        修改實例層級閾值（不動全域常數），以免影響其他 engine 實例或測試。
        """
        mode = (mode or "balanced").lower()
        if mode == "conservative":
            # 生產推薦：寧可慢也要對
            self._high_confidence = 78
            self._mid_confidence = 70
            self._mid_confirm_count = 3
            self._mid_min_buffer = 5
            self._stuck_soft_high = 70
            self._stuck_hard_high = 60
            self._stuck_soft_mid = 55
            self._boundary_high = 65
        elif mode == "aggressive":
            # 練習用：快但易漂移
            self._high_confidence = 60
            self._mid_confidence = 50
            self._mid_confirm_count = 1
            self._mid_min_buffer = 2
            self._stuck_soft_high = 50
            self._stuck_hard_high = 40
            self._stuck_soft_mid = 38
            self._boundary_high = 45
        else:  # balanced
            self._high_confidence = HIGH_CONFIDENCE
            self._mid_confidence = MID_CONFIDENCE
            self._mid_confirm_count = MID_CONFIRM_COUNT
            self._mid_min_buffer = MID_MIN_BUFFER_LEN
            self._stuck_soft_high = STUCK_SOFT_HIGH_CONFIDENCE
            self._stuck_hard_high = STUCK_HARD_HIGH_CONFIDENCE
            self._stuck_soft_mid = STUCK_SOFT_MID_CONFIDENCE
            self._boundary_high = BOUNDARY_HIGH_CONFIDENCE

    def estimate_speed(self) -> float:
        """根據最近 commits 估算語速（chars/sec）。"""
        if len(self._commit_history) < 2:
            return DEFAULT_READING_SPEED
        t0, c0 = self._commit_history[0]
        t1, c1 = self._commit_history[-1]
        elapsed = t1 - t0
        if elapsed < 0.1:
            return DEFAULT_READING_SPEED
        speed = (c1 - c0) / elapsed
        # 合理範圍：1-15 chars/sec
        return max(1.0, min(15.0, speed))

    def soft_time_advance(self, voice_active: bool) -> int:
        """講話中卡住時的時間軸軟推進。

        參數 voice_active：使用者是否正在講話（由外部 VAD/mic level 判斷）。
        只有在「正在講話 + 距離上次 commit 超過門檻」時才推進，每次最多
        SOFT_ADVANCE_MAX_CHARS 字元，以語速為依據估算合理位置。

        回傳：實際推進到的 global_char_pos（若無推進，回傳目前位置）。
        """
        if not self.sentences:
            return self.current_global_char
        if not voice_active:
            return self.current_global_char
        now = time.monotonic()
        elapsed = now - self._last_commit_time
        if elapsed < SOFT_ADVANCE_MIN_GAP_S:
            return self.current_global_char
        # 估算「按目前語速應該已經念過多少字」
        speed = self.estimate_speed()
        expected_chars = speed * elapsed
        # 別過頭：以 SOFT_ADVANCE_MAX_CHARS 為單次上限
        advance = int(min(SOFT_ADVANCE_MAX_CHARS, expected_chars))
        if advance < 1:
            return self.current_global_char
        new_pos = min(
            self.current_global_char + advance,
            self.transcript.total_chars if self.transcript else self.current_global_char,
        )
        # 不修改 _last_commit_time（這不是真正的 commit），只更新位置
        # 也記錄到 sentence_max_progress
        old_pos = self.current_global_char
        self.current_global_char = new_pos
        # 同步 sentence_index
        for i, s in enumerate(self.sentences):
            if s.start <= new_pos < s.end:
                self.current_sentence_index = i
                break
        # 軟推進是估算非辨識，視為低信心
        self._record_progress(self.current_sentence_index, new_pos, score=40)
        return new_pos

    def manual_mark_skipped_to_current(
        self, from_pos: int
    ) -> Optional[tuple[int, int]]:
        """手動標漏講：使用者觸發快捷鍵時，把 from_pos → current 之間標為漏講。"""
        if not self.sentences:
            return None
        start = max(0, min(from_pos, self.current_global_char))
        end = max(0, min(self.current_global_char, self.transcript.total_chars))
        if end <= start:
            return None
        return (start, end)

    def _locate_sentence(
        self, recognized_norm: str, *, expand: bool = False
    ) -> tuple[int, float]:
        """在當前位置附近的窗口內找最佳匹配句（字元 + 拼音 雙評分 + 距離權重）。

        距離權重：越接近目前位置加分越多，越遠離愈減分，避免短辨識文字因
        關鍵詞重複而命中遠處的句子造成「大跳段」。

        歧義處理：當近處與遠處兩個句子的原始分都極高（都 ≥ HIGH_CONFIDENCE）時，
        判定為歧義（如「Transformer」同時出現在多句），將信心降為中等以延遲 commit，
        等待後續 delta 來消除歧義。
        """
        cur = self.current_sentence_index
        ahead = self.jump_window if expand else self.window_ahead
        back = self.jump_window if expand else self.window_back
        lo = max(0, cur - back)
        hi = min(len(self.sentences), cur + ahead + 1)

        recognized_pinyin = to_pinyin_form(recognized_norm)
        rec_len = len(recognized_norm)

        # 第一階段：掃描所有候選，記錄每個的 raw、adjusted、proximity
        scored: list[tuple[int, float, float]] = []  # (idx, raw, adjusted)
        for i in range(lo, hi):
            sent = self.sentences[i]
            if not sent.normalized:
                continue
            raw = self._score_against(recognized_norm, recognized_pinyin, i)
            proximity = self._proximity_bonus(i - cur)
            gate_penalty = 0.0
            dist = abs(i - cur)

            # ✋ 使用者設定的最大跳段範圍（硬限制）
            forward_dist = i - cur
            if self._max_forward_sentences > 0 and forward_dist > self._max_forward_sentences:
                gate_penalty = 999.0  # 完全擋下（永遠輸給其他候選）
            elif self._max_forward_chars > 0:
                cand_char_forward = self.sentences[i].start - self.current_global_char
                if cand_char_forward > self._max_forward_chars:
                    gate_penalty = 999.0
            # 預設的大跳段閘門（配合短文字保護）
            elif dist >= BIG_JUMP_SENT_DIST:
                if rec_len < 2:
                    gate_penalty = 100.0
                elif rec_len < BIG_JUMP_MIN_REC_LEN:
                    if raw < 90:
                        gate_penalty = 100.0
                elif raw < BIG_JUMP_MIN_SCORE:
                    gate_penalty = 20.0
            adjusted = raw + proximity - gate_penalty
            scored.append((i, raw, adjusted))

        if not scored:
            return cur, 0.0

        best_idx, best_raw, _best_adjusted = max(scored, key=lambda x: x[2])
        return best_idx, max(0.0, best_raw)

    def _is_globally_ambiguous(self, search_text: str, recognized_pinyin: str) -> bool:
        """全域歧義偵測（軸 2 重寫）：基於「Top-2 候選分數差」而非絕對門檻。

        舊邏輯：所有 raw ≥ 85 + 距離 ≥ 3 的候選兩兩配對皆判歧義
          → 含重複詞的講稿（如多句都有 Transformer）幾乎永遠歧義 → 誤擋率 37%

        新邏輯：
          1. 收集所有 raw ≥ AMBIGUITY_MIN_SCORE (80) 的候選
          2. 找出「彼此距離 ≥ AMBIGUITY_DIST 的最高 2 名」
          3. 若兩者分數差 < AMBIGUITY_GAP (8) → 確實難分高下 → 歧義
          4. 若分數差 ≥ AMBIGUITY_GAP → 高分那個明顯勝出，不算歧義
        """
        candidates: list[tuple[int, float]] = []  # (sent_idx, raw)
        for i, sent in enumerate(self.sentences):
            if not sent.normalized:
                continue
            raw = self._score_against(search_text, recognized_pinyin, i)
            if raw >= AMBIGUITY_MIN_SCORE:
                candidates.append((i, raw))
        if len(candidates) < 2:
            return False
        # 排序：分數高的在前
        candidates.sort(key=lambda x: -x[1])
        top_idx, top_raw = candidates[0]
        # 找出與 top 距離 ≥ AMBIGUITY_DIST 的最高分候選
        for idx, raw in candidates[1:]:
            if abs(idx - top_idx) >= AMBIGUITY_DIST:
                # 兩者實力相近才算歧義
                return (top_raw - raw) < AMBIGUITY_GAP
        return False

    @staticmethod
    def _proximity_bonus(delta: int) -> float:
        """距離 delta 句（正=往前、負=往後）的分數加權。

        新版：強化「停留」偏好（+3）避免歧義詞誤跳到下一句。
        真正要往前推進時，下一句的高分自然會超過。
        """
        if delta == 0:
            return 3.0      # 強化停留偏好（避免歧義詞被誤推到下一句）
        if delta == 1:
            return 1.5      # 自然往下一句推進
        if delta == 2:
            return 0.5
        if delta > 0:
            return -1.0 * (delta - 2)
        return -2.0 * abs(delta)

    def _find_forward_skip_candidate(
        self, search_text: str
    ) -> Optional[tuple[int, float]]:
        """在「視窗結束後」掃描整份講稿，找出可能的主動跳段目標。

        當講者明確跳到很後面的段落時，視窗外的句子不會被 _locate_sentence
        看見，所以需要這個獨立掃描。為避免誤判，僅當 raw 分數很高時才回傳候選。
        """
        cur = self.current_sentence_index
        scan_start = cur + self.window_ahead + 1
        # 套用最大跳段範圍限制
        scan_end = len(self.sentences)
        if self._max_forward_sentences > 0:
            scan_end = min(scan_end, cur + self._max_forward_sentences + 1)
        if scan_start >= scan_end:
            return None
        recognized_pinyin = to_pinyin_form(search_text)
        best_idx = -1
        best_raw = -1.0
        for i in range(scan_start, scan_end):
            raw = self._score_against(search_text, recognized_pinyin, i)
            if raw > best_raw:
                best_raw = raw
                best_idx = i
        if best_idx >= 0 and best_raw >= BIG_JUMP_MIN_SCORE:
            return best_idx, best_raw
        return None

    def _global_locate(self, recognized_norm: str) -> tuple[int, float]:
        """全域搜尋：掃整份講稿找最佳匹配（只在卡住時使用）。"""
        recognized_pinyin = to_pinyin_form(recognized_norm)
        best_idx = self.current_sentence_index
        best_score = -1.0
        for i, sent in enumerate(self.sentences):
            if not sent.normalized:
                continue
            score = self._score_against(recognized_norm, recognized_pinyin, i)
            if score > best_score:
                best_score = score
                best_idx = i
        return best_idx, max(0.0, best_score)

    def _score_against(self, text_norm: str, text_pinyin: str, sent_idx: int) -> float:
        """用字元 + 拼音取最大值評分。"""
        from rapidfuzz import fuzz  # 局部 import 減少頂層依賴聲明
        sent = self.sentences[sent_idx]
        char_score = fuzz.partial_ratio(text_norm, sent.normalized)
        pinyin_score = 0.0
        if text_pinyin and sent_idx < len(self._sentence_pinyin):
            sp = self._sentence_pinyin[sent_idx]
            if sp:
                pinyin_score = fuzz.partial_ratio(text_pinyin, sp)
        return max(char_score, pinyin_score)

    def _char_align(
        self, sent_idx: int, recognized_norm: str, score: float = 100.0
    ) -> tuple[int, int]:
        """在指定句內做字元級對齊。"""
        try:
            return self._char_align_inner(sent_idx, recognized_norm, score)
        except Exception:
            # 任何 SequenceMatcher / pinyin 異常都回傳安全預設，避免應用當掉
            sent = self.sentences[sent_idx] if 0 <= sent_idx < len(self.sentences) else None
            if sent is None:
                return self.current_global_char, 0
            if sent.start <= self.current_global_char < sent.end:
                return self.current_global_char, 0
            return sent.start, 0

    def _char_align_inner(
        self, sent_idx: int, recognized_norm: str, score: float = 100.0
    ) -> tuple[int, int]:
        """實際對齊邏輯（被 _char_align 包裝以加異常保護）。"""
        sent = self.sentences[sent_idx]
        if not sent.normalized:
            return sent.start, 0

        matched_set = self._sentence_matched_chars.setdefault(sent_idx, set())
        phoneme_set = self._sentence_phoneme_matched_chars.setdefault(sent_idx, set())

        # === 字元層 ===
        matcher = SequenceMatcher(
            a=recognized_norm, b=sent.normalized, autojunk=False
        )
        best_block = None
        for block in matcher.get_matching_blocks():
            if block.size <= 0:
                continue
            for k in range(block.b, block.b + block.size):
                matched_set.add(k)
            if best_block is None or (block.b + block.size) > (best_block.b + best_block.size):
                best_block = block

        # === 拼音層（無論字元有無命中都跑，記錄拼音匹配的字元）===
        sent_tokens = (
            self._sentence_pinyin_tokens[sent_idx]
            if sent_idx < len(self._sentence_pinyin_tokens)
            else []
        )
        rec_tokens_with_pos = pinyin_tokens_with_positions(recognized_norm)
        rec_tokens = [t for t, _ in rec_tokens_with_pos]
        sent_token_strs = [t for t, _ in sent_tokens]
        if rec_tokens and sent_token_strs:
            pm = SequenceMatcher(a=rec_tokens, b=sent_token_strs, autojunk=False)
            for pblock in pm.get_matching_blocks():
                if pblock.size <= 0:
                    continue
                # 把匹配到的 sent_tokens 區段對應的 normalized 字元加入 phoneme_set
                for tok_idx in range(pblock.b, pblock.b + pblock.size):
                    end_pos_in_normalized = sent_tokens[tok_idx][1]
                    # 拼音 token 在 normalized 中可能對應 1 個漢字或數字英文塊
                    # 倒推：找到該 token 起始位置
                    start_pos = (
                        sent_tokens[tok_idx - 1][1] if tok_idx > 0 else 0
                    )
                    # 把 token 涵蓋的 normalized 字元都加入 phoneme_set
                    for k in range(start_pos, end_pos_in_normalized):
                        phoneme_set.add(k)

        if best_block is not None:
            consumed = best_block.a + best_block.size
            end_in_b = best_block.b + best_block.size
            return sent.normalized_to_global(end_in_b), consumed

        # 字元層無匹配 → 拼音層對齊
        sent_tokens = (
            self._sentence_pinyin_tokens[sent_idx]
            if sent_idx < len(self._sentence_pinyin_tokens)
            else []
        )
        if sent_tokens:
            rec_tokens_with_pos = pinyin_tokens_with_positions(recognized_norm)
            rec_tokens = [t for t, _ in rec_tokens_with_pos]
            sent_token_strs = [t for t, _ in sent_tokens]
            if rec_tokens and sent_token_strs:
                m = SequenceMatcher(a=rec_tokens, b=sent_token_strs, autojunk=False)
                best_pb = None
                for block in m.get_matching_blocks():
                    if block.size <= 0:
                        continue
                    if best_pb is None or (block.b + block.size) > (best_pb.b + best_pb.size):
                        best_pb = block
                if best_pb is not None:
                    end_pos_in_normalized = sent_tokens[best_pb.b + best_pb.size - 1][1]
                    consumed_tokens_end = best_pb.a + best_pb.size
                    # 把 token 結束位置換回 recognized_norm 的字元位置
                    consumed = (
                        rec_tokens_with_pos[consumed_tokens_end - 1][1]
                        if consumed_tokens_end > 0
                        else 0
                    )
                    return sent.normalized_to_global(end_pos_in_normalized), consumed

        # 都沒命中 → 不推進
        if sent.start <= self.current_global_char < sent.end:
            return self.current_global_char, 0
        return sent.start, 0

    def _trim_recent_buffer(self, consumed_chars: int) -> None:
        """匹配成功後修剪 recent_buffer：只保留未匹配到的尾段。

        避免同一段辨識文字持續匹配已經過去的句子。
        """
        if consumed_chars <= 0:
            return
        consumed_chars = min(consumed_chars, len(self._recent_buffer))
        self._recent_buffer = self._recent_buffer[consumed_chars:]
