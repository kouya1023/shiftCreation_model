from config import employees,work_types,time


#作成したシフトをまとめる
def summarize_shifts(solver,shifts):
    """ソルバーの計算結果を受け取り、従業員ごとの連続した勤務時間帯にまとめてフォーマットする。

    この関数は、ソルバーが割り当てた30分単位の不連続なシフトを受け取り、
    同一の業務（work_type）が連続している区間を「開始時刻 〜 終了時刻」の1つの勤務ブロックに結合します。
    これにより、数理モデルの出力データをフロントエンドの描画やCSV出力に適した
    人間が直感的に理解できる形式に変換します。

    Args:
        solver (cp_model.CpSolver): 最適化計算を完了したOR-Toolsのソルバーオブジェクト。
        shifts (dict): `{(worker_id, slot_idx, work_type_idx): BoolVar}` 形式の変数マップ。

    Returns:
        list: フォーマットされたシフト情報の二次元リスト。
            各要素は `[従業員名 (str), 開始時刻 (str), 終了時刻 (str), 業務名 (str)]` のリスト。
            例: [
                ["佐藤", "08:30", "12:00", "1番レジ"],
                ["鈴木", "13:00", "17:00", "売場業務"]
            ]

    Note:
        - 内部の `t_to_str` 関数により、スロットインデックス（基準 0 = 08:30）が時刻文字列へ変換されます。
        - 29番スロットの終了時刻（計算上は 23:30）に関しては、店舗の閉店時刻の運用ルールに合わせ、
          一律で `23:15` へ丸める特例処理が組み込まれています。
    """
    
    rows = []

    for e in employees:
        employee_name = employees[e][0]
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

                    #開店準備(8:30)の時間からの経過時間を求める
                    def t_to_str(t):
                        total_minutes = 510 + t * 30
                        h = total_minutes // 60
                        m = total_minutes % 60
                        res =  f"{h}:{m:02d}"

                        if res == "23:30":
                            res = "23:15"
                        return  res
                    
                    #出力用の「従業員名,業務開始時間,業務終了時間,業務名」を追加する
                    rows.append([
                        employee_name,
                        t_to_str(start_t),
                        t_to_str(end_t),
                        work_types.get(w,w),
                        
                    ])
                    if i < len(assigned_ts) - 1:
                        start_t = assigned_ts[i+1]
    

    
    return rows