from typing import List, Set, Dict, Any

class MissingEpisodeEngine:
    """统一缺集计算与区间压缩引擎 (API、Bot、前端共用同一实现)"""

    @staticmethod
    def calculate_missing(
        target_start: int,
        target_end: int,
        accepted_episodes: List[int]
    ) -> Dict[str, Any]:
        """
        计算缺集列表与缺集区间
        例如:
          target: 1-178, accepted: [7]
          missing individual: [1, 2, 3, 4, 5, 6, 8, ..., 178]
          missing ranges: ["1-6", "8-178"]
          completion percent: 0.56%
        """
        if target_start > target_end:
            target_start, target_end = target_end, target_start

        all_target_set: Set[int] = set(range(target_start, target_end + 1))
        accepted_set: Set[int] = set(accepted_episodes).intersection(all_target_set)
        missing_set: Set[int] = all_target_set - accepted_set

        missing_sorted = sorted(list(missing_set))
        total_target_count = len(all_target_set)
        accepted_count = len(accepted_set)
        
        completion_percent = round((accepted_count / total_target_count) * 100, 2) if total_target_count > 0 else 100.0

        # 压缩为友好区间表示
        ranges: List[str] = []
        if missing_sorted:
            start = missing_sorted[0]
            prev = start
            for x in missing_sorted[1:]:
                if x == prev + 1:
                    prev = x
                else:
                    if start == prev:
                        ranges.append(f"{start}")
                    else:
                        ranges.append(f"{start}-{prev}")
                    start = x
                    prev = x
            if start == prev:
                ranges.append(f"{start}")
            else:
                ranges.append(f"{start}-{prev}")

        return {
            "total_count": total_target_count,
            "accepted_count": accepted_count,
            "missing_count": len(missing_sorted),
            "completion_percent": completion_percent,
            "is_complete": len(missing_sorted) == 0,
            "missing_episodes": missing_sorted,
            "missing_ranges": ranges,
            "missing_ranges_formatted": "、".join(ranges) if ranges else "全剧已收录"
        }

missing_engine = MissingEpisodeEngine()
