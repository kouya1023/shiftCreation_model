import jpholiday
import pandas as pd
from datetime import date,timedelta
from ortools.sat.python import cp_model
import joblib
import numpy as np
from config import employees, work_types, time, employee_skills, max_in_month, max_in_day,task_time_windows,requested_holidays,employee_availability
from config import client
from db_exporter import upload_shifts_to_supabase




try:
    model = joblib.load('data/manager_ai_model.pkl')
    model_columns = joblib.load('data/model_columns.pkl')
    print("✅ 店長AIモデルを読み込みました")
except Exception as e:
    print(f"⚠️ AIモデルの読み込みに失敗しました: {e}")
    model = None

def get_ai_score(df, model, model_columns):
    if model is None:
        return 0
    
    if len(df) == 0:
        print("⚠️ シフトデータが空のため、AI評価をスキップします")
        return 0, []
    
    # 1. 時間を数値に変換
    def time_to_float(t_str):
        try:
            h, m = map(int, str(t_str).split(':'))
            return h + m / 60.0
        except: return 0.0

    temp_df = df.copy()
    temp_df['開始'] = temp_df['開始時間'].apply(time_to_float)
    temp_df['終了'] = temp_df['終了時間'].apply(time_to_float)

    # 2. 数値化 (One-Hot Encoding)
    # 学習時と同じ列を対象にします
    df_encoded = pd.get_dummies(temp_df, columns=['名前', '業務', '曜日', '従業員区分'])

    # 3. 列を学習時の52列（model_columns）に強制的に合わせる
    df_final = df_encoded.reindex(columns=model_columns, fill_value=0)
    # target列が混じっていたら削除
    if 'target' in df_final.columns:
        df_final = df_final.drop('target', axis=1)

    # 4. 予測 (クラス1: 採用 の確率をスコアとする)
    probs = model.predict_proba(df_final)
    # 全行の「採用確率」の平均を「店長満足度」とする
    scores = [p[1] for p in probs]
    avg_score = sum(scores) / len(scores) * 100
    
    return round(avg_score, 1), scores


def update_sheet_temp(dt, solver, shifts, accumulated_hours):
    """
    1日分のソルバー結果を、AIが判定できる30分単位のリストに変換する
    """
    day_label = f"{dt.month}/{dt.day}"
    weekday_idx = dt.weekday()
    weekday_str = ["月","火","水","木","金","土","日"][weekday_idx]
    if jpholiday.is_holiday(dt):
        weekday_str = "祝日"
    
    work_names = {0:"1番レジ", 1:"2番レジ", 2:"3番レジ", 3:"休憩", 4:"売り場業務", 5:"フルセルフ", 6:"一般食品", 7:"飲料補充", 8:"事務作業", 9:"MG", 10:"開店作業"}
    
    # その日に各人が何スロット働くか集計（月間労働時間のシミュレーション）
    today_slots = {e: 0 for e in employees}
    for (e, t, w) in shifts:
        if solver.Value(shifts[(e, t, w)]) == 1 and w != 3: # 休憩以外
            today_slots[e] += 1
            
    rows = []
    for (e, t, w) in shifts:
        if solver.Value(shifts[(e, t, w)]) == 1:
            emp_name = employees[e][0]
            category = employees[e][1]
            
            # 「今までの蓄積」＋「今日の予定」で、AIが見る「月間労働時間」を作る
            current_total_h = (accumulated_hours[e] + today_slots[e]) * 0.5
            
            def t_to_str(ts):
                total_minutes = 510 + ts * 30
                h, m = total_minutes // 60, total_minutes % 60
                res = f"{h}:{m:02d}"
                return "23:15" if res == "23:30" else res

            rows.append([
                f"{day_label}日",
                emp_name,
                t_to_str(t),
                t_to_str(t + 1),
                work_names.get(w, w),
                weekday_str,
                current_total_h,
                category
            ])
    return rows


def run_monthly_process(year,month):
    
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
    SHEET_NAME = f"{year}年{month}月"
    spreadsheet = client.open("Shift_App_DB")

    
    
    try:
        old_ws = spreadsheet.worksheet(SHEET_NAME)
        spreadsheet.del_worksheet(old_ws)
    except Exception:
        pass

    worksheet = spreadsheet.add_worksheet(title=SHEET_NAME,rows="1000",cols=8)

    all_rows = []
    all_30min_rows = []
    header = ["日付", "名前", "開始時間", "終了時間", "業務","曜日"]
    accumulated = {e:0 for e in employees}
    consecutive_counts = {e: 0 for e in employees}
    
    

    for dt in target_dates[:10]:

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
        sorted_e_ids = sorted(employees.keys(), key=lambda x: accumulated[x])
        solver,shifts,status = solve_single_day(year,day,month_of_day,accumulated,weekday_str,sorted_e_ids, is_weekend_or_holiday,consecutive_counts,model,model_columns)


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
            
            # 最初の日で失敗した場合は、詳細情報を表示
            if day == target_dates[0].day:
                print(f"\n   ⚠️ 最初の日で失敗しています。configを確認してください：")
                print(f"      1. employee_availability[従業員ID]['{weekday_str}'] の設定")
                print(f"      2. employee_skills[従業員ID] のスキル設定")
                print(f"      3. AIモデル（model）が正しく読み込まれているか")
           
        if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
                        
        # 3. 計算結果から、翌日のための連勤カウントを更新
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
            day_label = f"{month_of_day}/{day}"
            day_rows = update_sheet(day_label,weekday_str,solver,shifts)
            for e in employees:
                today_slots = sum(solver.Value(shifts[e,t,w])for t in time for w in [0,1,2,4,5,6,7,8,9,10] if (e,t,w)in shifts)
                accumulated[e] += today_slots 
            print(f"\n========== {day}日 ({weekday_str}) の確定シフト ==========")
            for row in day_rows:
                print(f"{row[1]:<8} | {row[2]} ～ {row[3]} | {row[4]}")
            all_rows.extend(day_rows)

            work_names = {0:"1番レジ", 1:"2番レジ", 2:"3番レジ", 3:"休憩", 4:"売り場業務", 5:"フルセルフ",6:"一般食品",7:"飲料補充",8:"事務作業",9:"MG",10:"開店作業"}

            for e in employees:
                emp_name = employees[e][0]            

                for t in time:
                    for w in work_types:
                        if(e,t,w) in shifts and solver.Value(shifts[(e,t,w)]) == 1:
                            def t_to_str(ts):
                                total_minutes = 510 + ts * 30
                                h, m = total_minutes // 60, total_minutes % 60
                                res = f"{h}:{m:02d}"
                                return "23:15" if res == "23:30" else res
                            all_30min_rows.append([
                                f"{day_label}日",
                                emp_name,
                                t_to_str(t),
                                t_to_str(t + 1),
                                work_names.get(w, w),
                                weekday_str,
                                e
                                
                            ])
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

    final_ml_rows = []
    
    final_totals_map = {}
    for e in employees:
        total_hours = 0
        # その月に計算された全日程の「30分単位の行」から、正確な時間を再計算
        for row in all_30min_rows:
            work_type = row[4].strip()
            if work_type != "休憩":
                if row[-1] == e: # row[-1] は保存しておいた従業員ID
                    # 「23:00」からのスロット（終了時間が23:15）なら0.25時間、それ以外は0.5時間
                    if row[2] == "23:00":
                        total_hours += 0.25
                    else:
                        total_hours += 0.5
        final_totals_map[e] = total_hours

    for row in all_30min_rows:
        e_id = row[6]
        actual_total = final_totals_map[e_id]
        category = employees[e_id][1]

        final_ml_rows.append([
            row[0], row[1], 
            row[2], row[3], # 数値版
            row[4], row[5],
            actual_total, # これが「1ヶ月分」の正しい合計！
            category
        ])

    df_proposed = pd.DataFrame(
        final_ml_rows, 
        columns=["日付", "名前", "開始時間", "終了時間", "業務", "曜日", "月間労働時間", "従業員区分"]
    )
    df_proposed['is_proposed'] = 1
    upload_shifts_to_supabase(year, month, df_proposed)


    # --- ここでAIに採点させる！ ---
    if model:
        total_score, row_scores = get_ai_score(df_proposed, model, model_columns)
        
        print(f"🌟 このシフトの店長満足度: {total_score}点")
        
        if total_score < 70:
            print("⚠️ 注意：店長が修正を入れる可能性が高いシフトです。")

    df_proposed['is_proposed'] = 1




    csv_filename = f"proposed_shift_{year}_{month}.csv"
    df_proposed.to_csv(csv_filename, index=False, encoding='utf-8-sig')
    print(f"✅ 学習用の30分単位AI提案データを保存しました: {csv_filename}")   
            
            
    worksheet.update([header]+all_rows)
    print(f"{SHEET_NAME} のシフトを保存しました！")

    proposed_totals_map= {employees[e][0]: accumulated[e] * 0.5 for e in employees}
    employee_category_map = {employees[e][0]:employees[e][1]for e in employees}


    ml_data_rows = []
    for row in all_rows:
        emp_name = row[1]
        # その従業員の合計労働時間時間を取得
        final_total_h = proposed_totals_map.get(emp_name, 0)
        employee_category = employee_category_map.get(emp_name,0)
        
        # 元のリストの末尾に合計時間を追加して、新しいリストを作成
        ml_data_rows.append(row + [final_total_h]+[employee_category])

    df_proposed = pd.DataFrame(ml_data_rows,columns=["日付", "名前", "開始時間", "終了時間", "業務", "曜日","月間労働時間","従業員区分"])
    df_proposed['is_proposed'] = 1

    csv_filename = f"proposed_shift_{year}_{month}.csv"
    df_proposed.to_csv(csv_filename,index=False,encoding='utf-8-sig')
    print(f"学習用のAI提案データを保存しました: {csv_filename}")

    return solver,shifts

    

def solve_single_day(year,day,month_of_day,accumulated_hours,current_weekday,sorted_e_ids,is_weekend_or_holiday,consecutive_counts,model_ai,model_columns,seed=42):
    
    
    
    
    model = cp_model.CpModel()
    


    target_date = date(year,month_of_day,day)
    is_holiday = jpholiday.is_holiday(target_date)
    is_weekend_or_holiday = (current_weekday in ["土","日"]) or is_holiday
    

    if is_holiday:
        h_name = jpholiday.is_holiday_name(target_date)
        print(f"祝日判定{month_of_day}/{day}は{h_name}です。")
        

    eval_data = []
    keys_list = []
    for e in employees:
        emp_name = employees[e][0]
        category = employees[e][1]
        possible_works = employee_skills.get(e, [])
        for t in  time:
            if t == 29:
                end_time = 23.25  # 23:15 終了にする
            else:
                end_time = 9.0 + t * 0.5 # 通常は 30分刻み
            for w in possible_works:
                work_name = work_types.get(w)
                eval_data.append({
                    '名前': emp_name,
                    '開始': 8.5 + t * 0.5,
                    '終了': end_time,
                    '業務': work_name,
                    '曜日': current_weekday,
                    '従業員区分': category
                })
                keys_list.append((e,t,w))
    #AIで採点（一括処理で高速化）
    eval_df = pd.get_dummies(pd.DataFrame(eval_data))
    eval_df = eval_df.reindex(columns=model_columns, fill_value=0)
    probs = model_ai.predict_proba(eval_df)
    raw_scores = probs[:, 1] + probs[:, 2] # 採用(1) + 修正(2) の確率
    
    score_map = {}
    for idx, key in enumerate(keys_list):
        score_map[key] = raw_scores[idx]
    current_windows = task_time_windows.copy()

    #平日ならば2,3番レジの開始時間を遅らせる
    if not is_weekend_or_holiday:
        current_windows[1] = (2,29) # 2番レジ：9:30〜
        current_windows[2] = (4,29) #3番レジ：10:30〜

    # 変数の定義
    shifts = {} 
    for e in sorted_e_ids:
        # 1. 曜日ごとの出勤可否と時間帯を取得
        avail = employee_availability.get(e, {}).get(current_weekday)
        
        # 固定休（availがNone）の場合は、この従業員の変数を一切作らない
        if avail is None:
            continue
            
        start_limit, end_limit = avail
        can_do_tasks = employee_skills.get(e, [])
        
        for w in work_types:
            # 休憩(w=3)は全員分作るが、それ以外はスキルがある人のみ
            if w != 3 and w not in can_do_tasks:
                continue
                
            start, end = current_windows.get(w, (0, 29))

            for t in time:
                # 2. 業務の稼働時間内、かつ「個人の労働可能時間内」である場合のみ変数を作成
                if (start <= t <= end) and (start_limit <= t <= end_limit):
                    shifts[(e, t, w)] = model.NewBoolVar(f'e{e}_d{day}_t{t}_w{w}')
    
    
        

    
    required_work = [0,4,5]#稼働時間に必ず割り当て：1番レジ、売場業務、セルフレジ
    #稼働時間に必須業務を割り当てる
    for t in time:
        for w in required_work:
            s,e = current_windows[w]
            if s <= t <= e:
                model.Add(sum(shifts[e,t,w]for e in sorted_e_ids if (e,t,w) in shifts) == 1)

        #9時から12時に事務業務を割り当てる
        if 0 <= t <= 6:
            model.Add(sum(shifts[e,t,8] for e in sorted_e_ids if (e,t,8)in shifts) == 1)
    
        #時間t業務wにつくのは一人(業務の重複を防ぐ)
        for w in [1,2]:
            model.Add(sum(shifts[(e,t,w)] for e in sorted_e_ids if (e,t,w)in shifts) <=1)       

    over_7h_flags = {}
    attendance_penalties= []
    all_attendance_flags = {}
    weekend_part_penalties = []
    continue_work_penalties =[]
    opening_penalties = []

    #月間上限を守らせるための動的制約
    for e in sorted_e_ids:
        category = employees[e][1]
        #今月の残り時間を計算
        remaining_limit = max_in_month[category] - accumulated_hours[e]
        #今日の上限（1日の上限と月間残枠の小さい方を採用）
        today_limit = min(max_in_day[category],remaining_limit)

        if today_limit < 0: today_limit = 0

        day_vars = [shifts[e,t,w] for t in time for w in work_types if w != 3 and (e,t,w) in shifts]

        model.Add(sum(day_vars) <= today_limit)


        #希望休の日は休ませる
        if (month_of_day,day) in requested_holidays.get(e,[]):
            if day_vars:
                model.Add(sum(day_vars) == 0)
        
        #勤務時間が6時間を超える場合には1時間休憩を取る
        if not day_vars:
            continue
        #実労働スロットの合計
        total_worked_slots = sum(day_vars)

        #４連勤ペナルティ
        if consecutive_counts[e] >= 3:
            weight = 1
            continue_work_penalties.append(weight)
        

        #7時間を超えるかどうかのフラグ
        is_over_7h = model.NewBoolVar(f'is_over_7h_e{e}_d{day}')
        over_7h_flags[(e, day)] = is_over_7h
        
        


        #total_worked_slots > 14ならば is_over_7h = 1
        model.Add(total_worked_slots >= 12).OnlyEnforceIf(is_over_7h)
        model.Add(total_worked_slots < 12).OnlyEnforceIf(is_over_7h.Not())
    
        #始業時間と終業時間を変数として定義
        start_slot = model.NewIntVar(min(time),max(time),f'start_e{e}_d{day}')
        end_slot = model.NewIntVar(min(time),max(time),f'end_e{e}_d{day}')
        
        tolerance = 6
        for t in time:
            if(e,t,3)in shifts:
                if (e, t, 3) in shifts:
                    is_break_t = shifts[(e,t,3)]

                    model.Add(2 * t>= start_slot + end_slot - tolerance).OnlyEnforceIf(is_break_t)
                    model.Add(2 * t<= start_slot + end_slot + tolerance).OnlyEnforceIf(is_break_t)
        
        
        # 各スロットtがON(1)なら、start_slotはそのt以下、end_slotはそのt以上になるように制約
        working_at_tlist = []
        starts= []

        #働いている時間tをis_working_listに追加する
        is_working_list = []
        for t in time:
            is_work = model.NewBoolVar(f'is_reg_e{e}_d{day}_t{t}')
            wok_sum = sum(shifts[e, t, w] for w in [0, 1, 2,4,5,6,7,8,9,10] if (e, t, w) in shifts)
            model.Add(is_work == wok_sum)
            is_working_list.append(is_work)
        

        for t in time:
            #work_sum:その時間tに職場にいるか            
            work_sum = sum(shifts[e,t,w] for w in work_types if (e,t,w) in shifts)
            is_working = model.NewBoolVar(f'_e{e}_d{day}_t{t}')
            model.Add(is_working == work_sum)
            working_at_tlist.append(is_working)
            model.Add(start_slot <= t).OnlyEnforceIf(is_working)
            model.Add(end_slot >= t).OnlyEnforceIf(is_working)
            #勤務の塊は１回のみ制約
            is_working = working_at_tlist[t]
            start_flag = model.NewBoolVar(f'start_f_e{e}_d{day}_t{t}')

            if t == 0:
                    model.Add(start_flag == is_working)
            else:
                prev_is_working = working_at_tlist[t-1]
                model.Add(start_flag >= is_working - prev_is_working)
            starts.append(start_flag)

        '''if e == 0: 
            model.AddHint(start_slot,9)'''



        max_starts = model.NewIntVar(0, 2, f'max_starts_e{e}_d{day}')
        model.Add(max_starts == 1 + is_over_7h).OnlyEnforceIf(is_over_7h)
        model.Add(sum(starts) <= 1 + is_over_7h)

        #is_working_somehow_flag:その日勤務しているか
        is_working_somehow_flag = model.NewBoolVar(f'is_working_somehow_e{e}_d{day}')
        '''if e in sorted_e_ids[:5]:
                model.AddHint(is_working_somehow_flag, 1)'''
        model.AddMaxEquality(is_working_somehow_flag, starts)
        
        category = employees[e][1]
        all_attendance_flags[e] = is_working_somehow_flag
        is_working = all_attendance_flags[e]
        acc = accumulated_hours[e]#*0.1
        if is_weekend_or_holiday:
            # パート・社員は 2000点（高くして、1人休みを誘導）
            # バイトは 200点（極端に低くして、朝や昼の隙間を埋めさせる）
            weight = 100 if category in ["boss", "part"] else 20
        else:
            weight = 50
            
        attendance_penalties.append(is_working * weight*acc)



        #1日の最低労働時間を設定
        model.Add(total_worked_slots >= 8).OnlyEnforceIf(is_working_somehow_flag)
        
        '''model.AddElement(start_slot, is_working_list, 1).OnlyEnforceIf(is_working_somehow_flag)
        model.AddElement(end_slot, is_working_list, 1).OnlyEnforceIf(is_working_somehow_flag)'''


        if is_weekend_or_holiday and employees[e][1] in ["boss", "part"]:
            # 「出勤している」かつ「7時間（14スロット）未満」の時に1になるフラグ
            is_short = model.NewBoolVar(f'is_short_e{e}_d{day}')
            
            # 1. 出勤していない場合は、ペナルティフラグは必ず 0
            model.Add(is_short == 0).OnlyEnforceIf(is_working_somehow_flag.Not())
            
            # 2. 出勤している場合：
            # 14スロット（6時間）未満ならフラグを 1 に強制
            model.Add(total_worked_slots < 12).OnlyEnforceIf([is_working_somehow_flag, is_short])
            # 14スロット（6時間）以上ならフラグを 0 に強制
            model.Add(total_worked_slots >= 12).OnlyEnforceIf([is_working_somehow_flag, is_short.Not()])
            
            # 1万点の強いペナルティをリストに追加
            weekend_part_penalties.append(is_short)
        
        

        break_starts = []
        for t in time:
            if (e,t,3) in shifts:
                is_break = shifts[(e,t,3)] 
                b_start = model.NewBoolVar(f'break_start_e{e}_d{day}_t{t}')

                if t == 0:
                    model.Add(b_start == is_break)
                else:
                    prev_break = sum([shifts[e, t-1, 3]] if (e, t-1, 3) in shifts else [])
                    model.Add(b_start >= is_break - prev_break)
                break_starts.append(b_start)

        model.Add(sum(break_starts) == 1).OnlyEnforceIf(is_over_7h)
        model.Add(sum(break_starts) == 0).OnlyEnforceIf(is_over_7h.Not())
    
        break_slots = sum(shifts[e,t,3] for t in time if (e,t,3) in shifts)

        #休憩スロットを入れる
        model.Add(break_slots == 2).OnlyEnforceIf(is_over_7h)
        model.Add(break_slots == 0).OnlyEnforceIf(is_over_7h.Not())

        #拘束時間を「実労働＋休憩2スロット」にピッタリ一致させる
        model.Add(end_slot - start_slot + 1 == total_worked_slots + break_slots).OnlyEnforceIf(is_working_somehow_flag)

    for t in time:
        for w in [6,7]:
            model.Add(sum(shifts[e,t,w] for e in sorted_e_ids if (e,t,w)in shifts)<= 1)

    break_point = sum(over_7h_flags[(e, day)] for e in employees if (e, day) in over_7h_flags)
    break_weight = 1000 if is_weekend_or_holiday else 2000

    #休憩時間が他の人重なるとペナルティ
    ''' break_overlap_penalties = []
    
    for t in time:
        num_on_break = model.NewIntVar(0,len(employees), f'num_brk_d{day}_t_{t}')
        model.Add(num_on_break == sum(shifts[e,t,3] for e in employees if (e,t,3)in shifts))

        penalty = model.NewIntVar(0,len(employees), f'brk_penalty_d{day}_t{t}')
        model.Add(penalty >= num_on_break -1)
        break_overlap_penalties.append(penalty)'''

    
    '''#レジに一人以上居なければならない
    target_work_types = [0,1,2]       
    


    for t in time:
        if t >=1:
            register_staff = sum(
                shifts[e,t,w]
                for e in employees
                for w in target_work_types
                if(e,t,w) in shifts
        )        
            model.Add(register_staff >=1)'''
    #業務の移り変わりを最小化
    switch_penalties = []
    for e in employees:
        for t in range(len(time)- 1):
            for w in work_types:
                for w_other in work_types:
                    if w == w_other:
                        continue
                    if (e,t,w) in shifts and (e,t+1,w_other) in shifts:
                        is_switch = model.NewBoolVar(f'sw_{e}_{t}_{w}_{w_other}')

                        model.Add(shifts[e,t,w] + shifts[e,t+1,w_other] - 1 <= is_switch)
                        switch_penalties.append(is_switch)

    #レジ数を減らすとペナルティ
    reg_penalties = []     

    for t in time:
            if t >= 1:
                if is_weekend_or_holiday:
                    target_num =2
                else:
                    if t >= 4: target_num = 2
                    elif t>= 2: target_num =1
                    else: target_num = 0
                num_on_reg = sum(shifts[(e,t,w)]for e in sorted_e_ids for w in [1,2]if (e,t,w) in shifts)  
                #model.Add(num_on_reg >= 1)
                shortage = model.NewIntVar(0,2,f'reg_shortage_t{t}')
                model.Add(num_on_reg + shortage >=(target_num ))
                reg_weight = 1000
                if t >= 25:
                    reg_weight = 500
                reg_penalties.append(shortage * reg_weight)

    #バッファ業務（品出し、事務)の設定
    flexible_task = [6,7,8]
    for w in flexible_task:
        start ,end  =current_windows[w]
        for t in time:
            if not (start <= t <= end):
                for e in employees:
                    if (e,t,w) in shifts:
                        model.Add(shifts[(e,t,w)] == 0)
    working_bonus = sum(shifts[e,t,w]for e in employees for t in time for w in [6,7] if (e,t,w) in shifts)
    office_bonus = sum(shifts[e,t,8]for e in employees for t in time  if (e,t,8) in shifts)

    #事務作業の割り当て、1日を通して一人まで
    is_admin_today = {}
    for e in employees:
        is_admin_today[e] = model.NewBoolVar(f'admin_flag_{e}')
        
        # どのスロット(t)であっても、事務(8)を割り当てられたらフラグを1にする
        # (shifts[e, t, 8]が1なら、is_admin_today[e]も必ず1になる)
        for t in time:
            if (e, t, 8) in shifts:
                model.Add(is_admin_today[e] >= shifts[e, t, 8])

    # 2. 事務(8)の担当フラグがONになる人を、全従業員の中で「合計1人」に制限
    model.Add(sum(is_admin_today[e] for e in employees) <= 1)
        
    if employee_availability[1][current_weekday] is not None and (month_of_day, day) not in requested_holidays.get(1, []):
        model.AddHint(is_admin_today[1] ,1)
    else:
        pass

    # MGは社員しかできないようにする
    for e in employees:
        if e != 0:
            for t in time:
                if (e, t, 9) in shifts:
                    model.Add(shifts[e, t, 9] == 0)
    mg_bonus = sum(shifts[0, t, 9] for t in time if (0, t, 9) in shifts) 

    #開店業務の割り当て
    opening_prep_vars = [shifts[e,0,10]for e in employees if (e,0,10)in shifts]
    if(opening_prep_vars):
        #開店作業は二人以上
        model.Add(sum(opening_prep_vars)>=1)
        
        is_shortage = model.NewBoolVar('opening_shortage_flag')
        model.Add(sum(opening_prep_vars) == 1).OnlyEnforceIf(is_shortage)
        model.Add(sum(opening_prep_vars) >= 2).OnlyEnforceIf(is_shortage.Not())
        opening_penalties.append(is_shortage)

    # 11:00(t=5) から 12:00(t=7) の休憩を促す
    early_break_slots = [5, 7]

    for e in employees:
        # 開店作業(w=10)を担当する人や、8:30から出勤している早番スタッフを対象にする
        # shifts[(e, 0, w)] が存在する人は早番と判断できます
        is_early_bird = any((e, 0, w) in shifts for w in work_types)
        
        if is_early_bird:
            for t in early_break_slots:
                if (e, t, 3) in shifts:
                    # この時間に休憩(w=3)を入れることをヒントとして与える
                    model.AddHint(shifts[(e, t, 3)], 1)
    # --- 3. 目的関数の設定 ---
    # AIスコアの合計（質）を最大化する
    # 整数しか扱えないため 100倍 してスケールを合わせます
    ai_compatibility_objective = sum(
        shifts[key] * int(score_map.get(key,0) *0.5 ) for key in shifts.keys()
        
    )

    


    model.Minimize(sum(switch_penalties)*5+  sum(attendance_penalties)  + sum(reg_penalties)+ sum(weekend_part_penalties)*1000 
                   + sum(opening_penalties)*100 - working_bonus*1 - break_point*break_weight -  mg_bonus*50
                   - office_bonus*50-is_admin_today[1]*100 -ai_compatibility_objective )
    #
    #+ sum(continue_work_penalties)
    #sum(break_overlap_penalties) +
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0 
    # solver設定に追加
    solver.parameters.random_seed = seed 
    solver.parameters.relative_gap_limit = 0.1  
    status = solver.Solve(model)

    if status == cp_model.OPTIMAL or status == cp_model.FEASIBLE:
        return solver, shifts,status
    else:   
        return None, None,None            
    




#スプレッドシートにレコードを追加する
def update_sheet(day,weekday_str,solver,shifts):
    
        
    #sheet = client.open("Shift_App_DB").worksheet("シフト下書き")
    
    
    rows = []
    #header = ["日付", "名前", "開始時間", "終了時間", "業務"]
    work_names = {0:"1番レジ", 1:"2番レジ", 2:"3番レジ", 3:"休憩", 4:"売り場業務", 5:"フルセルフ",6:"一般食品",7:"飲料補充",8:"事務作業",9:"MG",10:"開店作業"}

    for e in employees:
        employee_name = employees[e][0]
        #employee_category = employees[e][1]
        for w in work_types:
            assigned_ts = [t for t in time if (e,t,w)in shifts and solver.Value(shifts[(e,t,w)])== 1]
            if not assigned_ts:
                continue
            assigned_ts.sort()
            #連続する時間をひとまとめにする処理
            start_t = assigned_ts[0]
            for i in range(len(assigned_ts)):
                #連続が途切れるか、最後のスロットの場合に1行分を確定させる
                if i == len(assigned_ts) - 1 or assigned_ts[i+1] != assigned_ts[i] +1:
                    end_t = assigned_ts[i] +1

                    def t_to_str(t):
                        total_minutes = 510 + t * 30
                        h = total_minutes // 60
                        m = total_minutes % 60
                        res =  f"{h}:{m:02d}"

                        if res == "23:30":
                            res = "23:15"
                        return  res
                    
                    rows.append([
                        f"{day}日",
                        employee_name,
                        t_to_str(start_t),
                        t_to_str(end_t),
                        work_names.get(w,w),
                        weekday_str
                        
                    ])
                    if i < len(assigned_ts) - 1:
                        start_t = assigned_ts[i+1]
    

    
    return rows