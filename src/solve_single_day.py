from ortools.sat.python import cp_model
from datetime import date
import jpholiday
from config import employees,employee_skills,time,work_types,task_time_windows,employee_availability,max_in_month,max_in_day,requested_holidays


def solve_single_day(year,day,month_of_day,accumulated_hours,current_weekday,sorted_e_ids,is_weekend_or_holiday,consecutive_counts,seed=42):

    """制約プログラミング（OR-Tools CP-SAT）を用いて、特定日の最適な日間シフト（30分スロット単位）を生成する。

    本関数は、従業員のスキル、曜日ごとの出勤可能時間、月間累計労働時間による動的制限、
    および希望休を考慮した上で、店舗の運営に必要な各業務（レジ、売場、事務、開店等）の人員配置を決定します。
    また、6時間超勤務への1時間休憩や、曜日や時間ごとの必要レジ台数の変動など、実際の店舗ルールを制約式（Hard/Soft）としてモデル化しています。

    Args:
        year (int): シフト作成対象の年。
        day (int): シフト作成対象の日。
        month_of_day (int): シフト作成対象の月。
        accumulated_hours (dict): 各従業員の当月におけるこれまでの累計労働スロット数。
            キーは worker_id (int)、値は累積スロット数 (int)。
        current_weekday (str): 対象日の曜日文字列（"月", "火", "水", "木", "金", "土", "日", "祝日"）。
        sorted_e_ids (list): 動的優先度によってソートされた従業員ID（worker_id）のリスト。
        is_weekend_or_holiday (bool): 対象日が土日祝日であるかどうかのフラグ。
        consecutive_counts (dict): 各従業員の現在までの連続勤務日数。
            キーは worker_id (int)、値は連勤数 (int)。
        seed (int, optional): ソルバーの再現性を確保するためのランダムシード値。デフォルトは 42。

    Returns:
        tuple: 最適化の結果に応じた3つの要素のタプル。
            - solver (cp_model.CpSolver or None): 最適解または実行可能解が得られた場合はソルバーオブジェクト、解なしの場合は None。
            - shifts (dict or None): 生成されたシフト変数マップ `{(e, t, w): BoolVar}`。解なしの場合は None。
            - status (int or None): `cp_model.OPTIMAL` などのステータスコード。解なしの場合は None。

    Note:
        - 目的関数（Minimize）には、業務の移り変わり（一貫性の欠如）、必要レジ数の不足、土日祝日の社員・パートの短時間勤務、
          開店作業の人手不足などに対するペナルティが設定されており、ハード制約では解なしとなってしまうところを重みの値を変化させ試行錯誤しながら、
          より実際のシフトに近くなる値を設定しました。
        - 探索効率向上のため、特定の従業員へのヒント（`AddHint`）や、早番スタッフへの特定時間帯の休憩推奨ロジックが組み込まれています。
    """
    
    
    
    
    model = cp_model.CpModel()
    


    target_date = date(year,month_of_day,day)
    is_holiday = jpholiday.is_holiday(target_date)
    is_weekend_or_holiday = (current_weekday in ["土","日"]) or is_holiday
    

    if is_holiday:
        h_name = jpholiday.is_holiday_name(target_date)
        print(f"祝日判定{month_of_day}/{day}は{h_name}です。")
        
    
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

    over_6h_flags = {}
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
        

        #6時間を超えるかどうかのフラグ
        is_over_6h = model.NewBoolVar(f'is_over_6h_e{e}_d{day}')
        over_6h_flags[(e, day)] = is_over_6h
        
        


        #total_worked_slots > 12ならば is_over_7h = 1
        model.Add(total_worked_slots >= 12).OnlyEnforceIf(is_over_6h)
        model.Add(total_worked_slots < 12).OnlyEnforceIf(is_over_6h.Not())
    
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
        model.Add(max_starts == 1 + is_over_6h).OnlyEnforceIf(is_over_6h)
        model.Add(sum(starts) <= 1 + is_over_6h)

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

        model.Add(sum(break_starts) == 1).OnlyEnforceIf(is_over_6h)
        model.Add(sum(break_starts) == 0).OnlyEnforceIf(is_over_6h.Not())
    
        break_slots = sum(shifts[e,t,3] for t in time if (e,t,3) in shifts)

        #休憩スロットを入れる
        model.Add(break_slots == 2).OnlyEnforceIf(is_over_6h)
        model.Add(break_slots == 0).OnlyEnforceIf(is_over_6h.Not())

        #拘束時間を「実労働＋休憩2スロット」にピッタリ一致させる
        model.Add(end_slot - start_slot + 1 == total_worked_slots + break_slots).OnlyEnforceIf(is_working_somehow_flag)

    for t in time:
        for w in [6,7]:
            model.Add(sum(shifts[e,t,w] for e in sorted_e_ids if (e,t,w)in shifts)<= 1)

    break_point = sum(over_6h_flags[(e, day)] for e in employees if (e, day) in over_6h_flags)
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
    
    

    


    model.Minimize(sum(switch_penalties)*5+  sum(attendance_penalties)  + sum(reg_penalties)+ sum(weekend_part_penalties)*1000 
                   + sum(opening_penalties)*100 - working_bonus*1 - break_point*break_weight -  mg_bonus*50
                   - office_bonus*50-is_admin_today[1]*100  )
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