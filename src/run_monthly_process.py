import jpholiday
from datetime import date,timedelta
from ortools.sat.python import cp_model
from config import employees, work_types, time,  max_in_month, requested_holidays,employee_availability
from solve_single_day import solve_single_day
from summarize_shifts import summarize_shifts






def run_monthly_process(year,month):
    
    """main関数で指定された年月の月間シフトをOR-Toolsを用いて作成します。

    この関数は、1ヶ月の期間をループで回し、前日までの累計労働時間（accumulated）や
    連勤数（consecutive_counts）を動的に次の日のソルバー（solve_single_day）へ引き継ぐことで、
    月間バランスと連勤制限を考慮した日間最適化を連続的に実行します。

    また、平日と土日祝日で出勤させる従業員の属性（社員・パート ・ アルバイト）の
    プライオリティを切り替える「動的ソートロジック」を内部に備えています。

    Args:
        year (int): シフト作成を開始する対象の年（例: 2026）。
        month (int): シフト作成を開始する対象の月（21日からカウント開始)
            例: month=1 の場合、1月21日〜2月20日のシフトを生成します。

    Returns:
        tuple: 最終日の最適化オブジェクトと変数マップのタプル。
            - solver (cp_model.CpSolver or None): 最終日のソルバーオブジェクト（解なしの場合はNoneまたは直前の状態）。
            - shifts (dict or None): 最終日のシフト変数マップ `{(e, t, w): var}`。
    """
    
    start_date = date(year,month,21)
    if month == 12:
        end_date = date(year +1,1,20)
    else:
        end_date = date(year,month+1,20)

    target_dates = []
    current_date = start_date
    while current_date <= end_date:
        target_dates.append(current_date)
        current_date += timedelta(days=1)
    #実験用：target_dates = [date(2026,1,24)]
    
    
    all_rows = []
    
    accumulated = {e:0 for e in employees}
    consecutive_counts = {e: 0 for e in employees}
    
    

    for dt in target_dates:

        is_holiday = jpholiday.is_holiday(dt)
        is_weekend_or_holiday = (dt.weekday() >= 5) or is_holiday

        # 動的ソートロジック
        def get_sort_priority(e_id):
            category = employees[e_id][1]
            acc = accumulated[e_id]
            
            if is_weekend_or_holiday:
                # 土日祝：バイトを優先（優先度0）、その他（優先度1）
                # その中で労働時間が少ない順
                prio = 0 if category == "arubaito" else 1
                return (prio, acc)
            else:
                # 平日：boss（社員等）とパートを優先（優先度0）、その他（優先度1）
                prio = 0 if category in ["boss", "part"] else 1
                return (prio, acc)

        sorted_e_ids = sorted(employees.keys(), key=get_sort_priority)
        
        


        day = dt.day
        month_of_day = dt.month
        year = dt.year
        weekday_idx = dt.weekday()
        weekday_str = ["月","火","水","木","金","土","日"][weekday_idx]
        if is_holiday:
            weekday_str = "祝日"
        solver,shifts,status = solve_single_day(year,day,month_of_day,accumulated,weekday_str,sorted_e_ids, is_weekend_or_holiday,consecutive_counts)


        if status != cp_model.OPTIMAL and status != cp_model.FEASIBLE:
            print(f"\n❌ {month_of_day}月{day}日 ({weekday_str}): 解なし")
            print(f"   ステータスコード: {status}")
            
            # 出勤可能人数を確認
            available_count = 0
            for e in sorted_e_ids:
                avail = employee_availability.get(e, {}).get(weekday_str)
                is_holiday_req = (month_of_day, day) in requested_holidays.get(e, [])
                remaining = max_in_month[employees[e][1]] - accumulated[e]
                
                if avail is not None and not is_holiday_req and remaining >= 6:
                    available_count += 1
            
            print(f"   出勤可能人数: {available_count}人")
            
            
           
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
                        
        # 計算結果から、翌日のための連勤カウントを更新
            for e in sorted_e_ids:
                # その日に少しでも働いたか（is_working_somehow_flag に相当する変数を確認）
                # shifts[(e, t, w)] のいずれかが 1 なら出勤
                worked_today = False
                for t in range(30): # timeスロット
                    for w in work_types:
                        if (e, t, w) in shifts and solver.Value(shifts[(e, t, w)]) == 1:
                            worked_today = True
                            break
                    if worked_today: break
                
                if worked_today:
                    consecutive_counts[e] += 1
                else:
                    consecutive_counts[e] = 0 # 休んだらリセット

        if solver:
            day_rows = summarize_shifts(solver,shifts)
            for e in employees:
                today_slots = sum(solver.Value(shifts[e,t,w])for t in time for w in [0,1,2,4,5,6,7,8,9,10] if (e,t,w)in shifts)
                accumulated[e] += today_slots
  
            print(f"\n========== {day}日 ({weekday_str}) の確定シフト ==========")

            for row in day_rows:
                print(f"{row[0]:<8} | {row[1]} ～ {row[2]} | {row[3]}")
            all_rows.extend(day_rows)
     
        else:
            print(f"⚠️ {day}日は解なし：スタッフ不足の可能性があります")
            # その日に出勤可能かつ、月間上限に達していない人をカウント
            available_staff_count = 0
            remaining_hours_sum = 0
            for e in employees:
                # 休み希望ではなく、かつ月間上限まで1時間以上残っているか
                if day not in requested_holidays.get(e, []) and (max_in_month[employees[e][1]] - accumulated[e]) >= 1:
                    available_staff_count += 1
                    remaining_hours_sum += (max_in_month[employees[e][1]] - accumulated[e])
            
            print(f"   - 出勤可能人数: {available_staff_count}人")
            print(f"   - チーム全体の残り労働枠: {remaining_hours_sum}時間")


    return solver,shifts

    

       
    




