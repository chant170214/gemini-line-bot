import uuid

# 生成するコードの数
NUM_CODES_TO_GENERATE = 5

# 有効なコードを保存するファイル
CODE_FILE = 'valid_codes.txt'

def generate_and_save_codes():
    """新しいワンタイムコードを生成してファイルに追記する"""
    new_codes = []
    for _ in range(NUM_CODES_TO_GENERATE):
        # 8文字のランダムなコードを生成
        code = str(uuid.uuid4())[:8]
        new_codes.append(code)

    try:
        with open(CODE_FILE, 'a') as f:
            for code in new_codes:
                f.write(code + '\n')
        
        print(f"{NUM_CODES_TO_GENERATE}個の新しいコードが生成されました。")
        print("--- 生成されたコード ---")
        for code in new_codes:
            print(code)
        print("------------------------")
        print(f"これらのコードは '{CODE_FILE}' に保存されました。")

    except IOError as e:
        print(f"エラー: ファイル '{CODE_FILE}' に書き込めませんでした。")
        print(e)

if __name__ == "__main__":
    generate_and_save_codes()