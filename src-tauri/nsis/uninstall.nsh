; ClipMind NSIS 卸载钩子
; 在卸载流程开始前弹窗询问用户是否保留数据（收藏夹、知识库）。
;
; 数据目录（Windows）： %APPDATA%\ClipMind\data  （含 models 子目录，需保留）
; 默认行为：保留数据（NSIS 默认只删安装目录，不碰 APPDATA）。
; 用户选择"否"时清理数据目录，但保留 models/ 子目录（避免重新下载 ASR 模型）。

!macro NSIS_HOOK_PREUNINSTALL
  ; MessageBox 返回值：IDYES = 用户点击"是"，IDNO = 用户点击"否"
  ; 语义：点击"是"保留数据，点击"否"彻底删除（保留 ASR 模型）
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "是否保留 ClipMind 的数据（收藏夹、知识库）？$\n$\n点击$\"是$\"保留数据，点击$\"否$\"彻底删除（保留 ASR 模型）。" \
    IDYES skip_delete_data

    ; 用户选择"否"：清理数据目录但保留 models 子目录
    ; 遍历 %APPDATA%\ClipMind\data\* 下所有项，跳过 models，其余文件/目录分别删除
    Push $0
    Push $1
    IfFileExists "$APPDATA\ClipMind\data\*.*" 0 done_delete_data
      ClearErrors
      FindFirst $0 $1 "$APPDATA\ClipMind\data\*"
      loop_data:
        StrCmp $1 "" done_loop
        StrCmp $1 "models" next_item
        ; 判断当前项是目录还是文件（目录可通过 \*.* 命中）
        IfFileExists "$APPDATA\ClipMind\data\$1\*.*" 0 is_file
          RMDir /r "$APPDATA\ClipMind\data\$1"
          Goto next_item
        is_file:
          Delete "$APPDATA\ClipMind\data\$1"
        next_item:
        FindNext $0 $1
        Goto loop_data
      done_loop:
      FindClose $0
      DetailPrint "已清理 ClipMind 数据目录（保留 models）: $APPDATA\ClipMind\data"
    done_delete_data:
    Pop $1
    Pop $0

  skip_delete_data:
    ; 用户选择"是"或目录不存在：保留数据，不做任何操作
!macroend
