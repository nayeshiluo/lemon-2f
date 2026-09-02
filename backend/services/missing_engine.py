from typing import List, Set, Dict, Any, Tuple, Optional

class MissingEpisodeEngine:
    """
    统一多季剧集缺集计算与区间压缩引擎 (支持按季独立计算、格式化输出与进度汇总)
    """

    @staticmethod
    def _compress_ranges(numbers: List[int]) -> List[str]:
        """将有序整数列表压缩为区间表示，例如 [1, 2, 3, 5] -> ['1-3', '5']"""
        if not numbers:
            return []
        sorted_nums = sorted(list(set(numbers)))
        ranges: List[str] = []
        start = sorted_nums[0]
        prev = start
        for x in sorted_nums[1:]:
            if x == prev + 1:
                prev = x
            else:
                ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
                start = x
                prev = x
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        return ranges

    @classmethod
    def calculate_multi_season_missing(
        cls,
        season_definitions: Dict[int, int], # season_number -> total_episodes, e.g. {1: 12, 2: 24}
        accepted_records: List[Tuple[int, int]] # list of (season, episode)
    ) -> Dict[str, Any]:
        """
        多季精准缺集计算
        返回值包含:
          - seasons_detail: 各季独立总数、已收录、缺失列表、区间
          - total_episodes: 全剧总集数
          - total_accepted: 全剧已收录集数
          - total_missing: 全剧缺失集数
          - completion_percent: 全剧完成百分比
          - formatted_summary: "S01E03-E06、S01E08-E12、S02E03-E24"
        """
        accepted_set: Set[Tuple[int, int]] = set(accepted_records)
        seasons_detail: Dict[int, Dict[str, Any]] = {}
        
        all_missing_formatted_parts: List[str] = []
        grand_total = 0
        grand_accepted = 0

        for season_num in sorted(season_definitions.keys()):
            ep_count = season_definitions[season_num]
            grand_total += ep_count

            target_eps = set(range(1, ep_count + 1))
            season_accepted = {ep for (s, ep) in accepted_set if s == season_num and ep in target_eps}
            grand_accepted += len(season_accepted)

            season_missing = sorted(list(target_eps - season_accepted))
            ranges = cls._compress_ranges(season_missing)

            seasons_detail[season_num] = {
                "season": season_num,
                "total_episodes": ep_count,
                "accepted_count": len(season_accepted),
                "missing_count": len(season_missing),
                "is_complete": len(season_missing) == 0,
                "missing_episodes": season_missing,
                "missing_ranges": ranges
            }

            # 格式化季集区间: 如 S01E03-E06, S01E08
            for r in ranges:
                if "-" in r:
                    start_ep, end_ep = r.split("-")
                    all_missing_formatted_parts.append(f"S{season_num:02d}E{int(start_ep):02d}-E{int(end_ep):02d}")
                else:
                    all_missing_formatted_parts.append(f"S{season_num:02d}E{int(r):02d}")

        grand_missing = grand_total - grand_accepted
        completion_percent = round((grand_accepted / grand_total) * 100, 2) if grand_total > 0 else 100.0

        return {
            "total_count": grand_total,
            "accepted_count": grand_accepted,
            "missing_count": grand_missing,
            "completion_percent": completion_percent,
            "is_complete": grand_missing == 0,
            "seasons_detail": seasons_detail,
            "missing_ranges_formatted": "、".join(all_missing_formatted_parts) if all_missing_formatted_parts else "全剧已收录"
        }

missing_engine = MissingEpisodeEngine()
