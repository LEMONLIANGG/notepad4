/******************************************************************************
*
*
* Notepad4
*
* FolderBrowser.h
*   Built-in folder browser side panel (a lightweight matepath inside the
*   main window). Selecting a file loads it into the current window instead
*   of launching a new browser process.
*
* See License.txt for details about distribution and modification.
*
*
******************************************************************************/
#pragma once

// Panel state, persisted in Notepad4.ini (see LoadSettings/SaveSettings).
// iFolderBrowserWidth is in logical pixels (96 DPI).
extern bool bShowFolderBrowser;
extern int iFolderBrowserWidth;

void FolderBrowser_Create(HWND hwnd, HINSTANCE hInstance) noexcept;
void FolderBrowser_Destroy() noexcept;
void FolderBrowser_SetVisible(HWND hwnd, bool bShow) noexcept;
void FolderBrowser_Toggle(HWND hwnd) noexcept;

// Width occupied by the panel including the splitter; 0 when hidden.
int FolderBrowser_GetWidth() noexcept;

// Position the panel controls inside the given rectangle, and the editor is
// expected to start at x + FolderBrowser_GetWidth().
void FolderBrowser_Layout(int x, int y, int cx, int cy) noexcept;
void FolderBrowser_OnDpiChanged() noexcept;

// Returns true when the notification came from the folder browser controls.
bool FolderBrowser_HandleNotify(HWND hwnd, LPARAM lParam) noexcept;

// Follow the editor: show the folder of the given file in the panel.
void FolderBrowser_SyncToFile(LPCWSTR pszFile) noexcept;

// 备注内联编辑的开启/收尾（双击备注列、Enter/Esc、失焦都会投递到这里）。
// action: 0 = 取消，1 = 确认并回到列表焦点，2 = 确认，3 = 在 lParam 指定的条目上开始编辑
void FolderBrowser_EndNoteEdit(WPARAM action, LPARAM lParam) noexcept;

// Ctrl+C / Ctrl+Shift+C 落到主窗口时，如果焦点在文件列表上就复制文件名/完整路径
// （返回 true 表示已经处理，主窗口不要再走编辑器自己的复制）。
bool FolderBrowser_CopyFromList(bool bFullPath) noexcept;

// 重新读取当前目录（Ctrl+R）
void FolderBrowser_Refresh() noexcept;
