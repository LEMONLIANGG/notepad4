// This file is part of Notepad4.
// See License.txt for details about distribution and modification.

#include <windows.h>
#include <windowsx.h>
#include <shlwapi.h>
#include <shlobj.h>
#include <shellapi.h>
#include <commctrl.h>
#include <uxtheme.h>

#include "SciCall.h"
#include "config.h"
#include "Helpers.h"
#include "Dialogs.h"
#include "DarkMode.h"
#include "Notepad4.h"
#include "FolderBrowser.h"

extern HWND hwndMain;

namespace {

// 面板宽度以 96 DPI 为基准的逻辑像素记录在 INI 中，运行时按窗口 DPI 换算。
constexpr int FOLDER_BROWSER_DEFAULT_WIDTH			= 400;
constexpr int FOLDER_BROWSER_MIN_WIDTH				= 130;
constexpr int FOLDER_BROWSER_MIN_EDIT_WIDTH			= 240;
constexpr int FOLDER_BROWSER_DEFAULT_TREE_HEIGHT	= 240;
constexpr int FOLDER_BROWSER_MIN_TREE_HEIGHT		= 48;
constexpr int FOLDER_BROWSER_MIN_LIST_HEIGHT		= 48;
constexpr int FOLDER_BROWSER_SPLITTER_THICKNESS		= 5;
constexpr int FOLDER_BROWSER_MAX_ENTRIES			= 1000;

// 文件列表的列（显示「详细信息」里的几项；Comment 可直接编辑 = 对文件备注）
enum FolderListColumn {
	FolderListColumn_Name = 0,
	FolderListColumn_Size,
	FolderListColumn_Date,
	FolderListColumn_Attributes,
	FolderListColumn_Comment,
	FolderListColumn_Count
};

// 默认面板宽度正好放得下这几列：Name 吃剩余空间，其余固定
constexpr int FOLDER_LIST_MIN_NAME_WIDTH	= 80;
constexpr int FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Count] = {
	FOLDER_LIST_MIN_NAME_WIDTH,	// Name（占剩余空间）
	65,							// Size
	95,							// Date modified
	40,							// Attributes
	115,						// Comment
};

// 备注存在文件的 NTFS 备用数据流里（同类子「属性 -> 详细信息 -> 备注」，文件自带、随文件走）
#define FOLDER_BROWSER_NOTE_STREAM	L":Notepad4.Note"
constexpr int FOLDER_BROWSER_NOTE_MAX = 4000;	// 备注最大字符数

#define WC_FOLDER_BROWSER_SPLITTER	L"NP2FolderBrowserSplitter"

// 右键菜单命令
enum FolderBrowseCommand {
	FolderBrowseCommand_CopyName = 1,
	FolderBrowseCommand_CopyPath,
	FolderBrowseCommand_EditComment,
	FolderBrowseCommand_ClearComment,
	FolderBrowseCommand_Refresh,
};

HWND hwndFolderTree = nullptr;
HWND hwndFolderList = nullptr;
HWND hwndSplitterV = nullptr;
HWND hwndSplitterH = nullptr;
HWND hwndNoteEdit = nullptr;
HFONT hFontFolder = nullptr;
HIMAGELIST himlFolder = nullptr;

int cxFolderPanel = 0;	// 树/列表区宽度（物理像素）
int cyFolderTree = 0;	// 树的高度（物理像素）

bool bFolderSyncing = false;
WCHAR szFolderDir[MAX_PATH];

// 内联编辑备注用
int iNoteEditItem = -1;
WCHAR szNoteEditPath[MAX_PATH];
bool bNoteEditEnding = false;

bool bDraggingV = false;
bool bDraggingH = false;
int iDragOrigin = 0;
int iDragStartValue = 0;

struct FolderEntry {
	WCHAR name[MAX_PATH];
	bool isDir;
	DWORD dwFileAttributes;
	ULONGLONG size;
	FILETIME ftLastWrite;
};

int ScaleForDpi(int value) noexcept {
	const UINT dpi = hwndMain != nullptr ? GetWindowDPI(hwndMain) : USER_DEFAULT_SCREEN_DPI;
	return MulDiv(value, static_cast<int>(dpi), USER_DEFAULT_SCREEN_DPI);
}

int __cdecl CmpFolderEntry(const void *p1, const void *p2) noexcept {
	const FolderEntry *const a = static_cast<const FolderEntry *>(p1);
	const FolderEntry *const b = static_cast<const FolderEntry *>(p2);
	if (a->isDir != b->isDir) {
		return a->isDir ? -1 : 1;
	}
	return StrCmpLogicalW(a->name, b->name);
}

int IconIndexForEntry(bool bDir) noexcept {
	constexpr DWORD iconFlags = SHGFI_USEFILEATTRIBUTES | SHGFI_SMALLICON | SHGFI_SYSICONINDEX;
	static int iIconFolder = -1;
	static int iIconFile = -1;
	int &iCached = bDir ? iIconFolder : iIconFile;
	if (iCached < 0) {
		SHFILEINFO shfi;
		SHGetFileInfo2(L"Icon", bDir ? FILE_ATTRIBUTE_DIRECTORY : FILE_ATTRIBUTE_NORMAL,
					   &shfi, sizeof(SHFILEINFO), iconFlags);
		iCached = shfi.iIcon;
	}
	return iCached;
}

// 收集目录下的条目（列表用：目录 + 文件；树用：只收目录），返回条数。
int CollectEntries(LPCWSTR pszDir, bool bDirectoriesOnly, FolderEntry **ppEntries) noexcept {
	*ppEntries = nullptr;

	WCHAR szPattern[MAX_PATH];
	PathCombine(szPattern, pszDir, L"*");

	WIN32_FIND_DATA fd;
	HANDLE hFind = FindFirstFile(szPattern, &fd);
	if (hFind == INVALID_HANDLE_VALUE) {
		return 0;
	}

	constexpr DWORD dwSkipMask = FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM;
	FolderEntry *pEntries = nullptr;
	int count = 0;
	int capacity = 0;
	do {
		const bool bDir = (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
		if (bDirectoriesOnly && !bDir) {
			continue;
		}
		if ((fd.dwFileAttributes & dwSkipMask) != 0) {
			continue;
		}
		if (fd.cFileName[0] == L'.' && (fd.cFileName[1] == L'\0' || (fd.cFileName[1] == L'.' && fd.cFileName[2] == L'\0'))) {
			continue;
		}
		if (count >= FOLDER_BROWSER_MAX_ENTRIES) {
			break;
		}
		if (count == capacity) {
			const int newCapacity = capacity ? (capacity * 2) : 64;
			FolderEntry *pNew = static_cast<FolderEntry *>(NP2HeapAlloc(static_cast<size_t>(newCapacity) * sizeof(FolderEntry)));
			if (pNew == nullptr) {
				break;
			}
			if (pEntries != nullptr) {
				memcpy(pNew, pEntries, static_cast<size_t>(count) * sizeof(FolderEntry));
				NP2HeapFree(pEntries);
			}
			pEntries = pNew;
			capacity = newCapacity;
		}
		lstrcpyn(pEntries[count].name, fd.cFileName, MAX_PATH);
		pEntries[count].isDir = bDir;
		pEntries[count].dwFileAttributes = fd.dwFileAttributes;
		pEntries[count].size = (static_cast<ULONGLONG>(fd.nFileSizeHigh) << 32) | fd.nFileSizeLow;
		pEntries[count].ftLastWrite = fd.ftLastWriteTime;
		++count;
	} while (FindNextFile(hFind, &fd));
	FindClose(hFind);

	if (pEntries != nullptr) {
		qsort(pEntries, static_cast<size_t>(count), sizeof(FolderEntry), CmpFolderEntry);
	}
	*ppEntries = pEntries;
	return count;
}

// ---------------------------------------------------------------------------
// 文件详细信息 / 备注（NTFS 备用数据流）
// ---------------------------------------------------------------------------

void FormatFileSize(ULONGLONG size, LPWSTR pszText, int cchText) noexcept {
	if (size < 1024) {
		wsprintf(pszText, L"%u B", static_cast<unsigned>(size));
	} else if (size < 1024ull * 1024) {
		const unsigned t = static_cast<unsigned>((size * 10 + 512) / 1024);
		wsprintf(pszText, L"%u.%u KB", t / 10, t % 10);
	} else if (size < 1024ull * 1024 * 1024) {
		const unsigned t = static_cast<unsigned>((size * 10 + 512 * 1024) / (1024 * 1024));
		wsprintf(pszText, L"%u.%u MB", t / 10, t % 10);
	} else {
		const unsigned t = static_cast<unsigned>((size * 10 + 512ull * 1024 * 1024) / (1024ull * 1024 * 1024));
		wsprintf(pszText, L"%u.%u GB", t / 10, t % 10);
	}
	pszText[cchText - 1] = L'\0';
}

void FormatFileTime(const FILETIME &ft, LPWSTR pszText, int cchText) noexcept {
	pszText[0] = L'\0';
	FILETIME ftLocal;
	SYSTEMTIME st;
	if (!FileTimeToLocalFileTime(&ft, &ftLocal) || !FileTimeToSystemTime(&ftLocal, &st)) {
		return;
	}
	WCHAR szDate[64] = L"";
	WCHAR szTime[64] = L"";
	GetDateFormatW(LOCALE_USER_DEFAULT, DATE_SHORTDATE, &st, nullptr, szDate, COUNTOF(szDate));
	GetTimeFormatW(LOCALE_USER_DEFAULT, TIME_NOSECONDS, &st, nullptr, szTime, COUNTOF(szTime));
	wsprintf(pszText, L"%s %s", szDate, szTime);
	pszText[cchText - 1] = L'\0';
}

// 与资源管理器「属性」列同一套字母（目录 D、只读 R、隐藏 H、系统 S、存档 A、链接 L、压缩 C、加密 E）
void FormatFileAttributes(DWORD dwAttributes, LPWSTR pszText, int cchText) noexcept {
	int n = 0;
	const struct { DWORD mask; WCHAR ch; } items[] = {
		{ FILE_ATTRIBUTE_DIRECTORY, L'D' },
		{ FILE_ATTRIBUTE_READONLY, L'R' },
		{ FILE_ATTRIBUTE_HIDDEN, L'H' },
		{ FILE_ATTRIBUTE_SYSTEM, L'S' },
		{ FILE_ATTRIBUTE_ARCHIVE, L'A' },
		{ FILE_ATTRIBUTE_REPARSE_POINT, L'L' },
		{ FILE_ATTRIBUTE_COMPRESSED, L'C' },
		{ FILE_ATTRIBUTE_ENCRYPTED, L'E' },
	};
	for (const auto &item : items) {
		if (n + 1 >= cchText) {
			break;
		}
		if ((dwAttributes & item.mask) != 0) {
			pszText[n++] = item.ch;
		}
	}
	pszText[n] = L'\0';
}

void MakeNoteStreamPath(LPCWSTR pszPath, LPWSTR pszStream, int cchStream) noexcept {
	lstrcpyn(pszStream, pszPath, cchStream);
	StrCatBuff(pszStream, FOLDER_BROWSER_NOTE_STREAM, cchStream);
}

bool ReadFileNote(LPCWSTR pszPath, LPWSTR pszNote, int cchNote) noexcept {
	pszNote[0] = L'\0';
	WCHAR szStream[MAX_PATH + 32];
	MakeNoteStreamPath(pszPath, szStream, COUNTOF(szStream));

	const HANDLE hFile = CreateFile(szStream, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE,
									nullptr, OPEN_EXISTING, 0, nullptr);
	if (hFile == INVALID_HANDLE_VALUE) {
		return false;
	}
	char buf[FOLDER_BROWSER_NOTE_MAX * 3 + 4];
	DWORD cbRead = 0;
	const BOOL bRead = ReadFile(hFile, buf, sizeof(buf) - 1, &cbRead, nullptr);
	CloseHandle(hFile);
	if (!bRead) {
		return false;
	}
	buf[cbRead] = '\0';
	MultiByteToWideChar(CP_UTF8, 0, buf, -1, pszNote, cchNote - 1);
	pszNote[cchNote - 1] = L'\0';
	return pszNote[0] != L'\0';
}

bool WriteFileNote(LPCWSTR pszPath, LPCWSTR pszNote) noexcept {
	WCHAR szStream[MAX_PATH + 32];
	MakeNoteStreamPath(pszPath, szStream, COUNTOF(szStream));

	if (StrIsEmpty(pszNote)) {
		// 清空备注 = 删掉数据流（本来就没有也算成功）
		if (DeleteFile(szStream)) {
			return true;
		}
		const DWORD dwError = GetLastError();
		return dwError == ERROR_FILE_NOT_FOUND || dwError == ERROR_PATH_NOT_FOUND;
	}

	char utf8[FOLDER_BROWSER_NOTE_MAX * 3 + 4];
	const int cbUtf8 = WideCharToMultiByte(CP_UTF8, 0, pszNote, -1, utf8, sizeof(utf8), nullptr, nullptr);
	if (cbUtf8 <= 1) {
		return true;
	}

	const HANDLE hFile = CreateFile(szStream, GENERIC_WRITE, FILE_SHARE_READ,
									nullptr, CREATE_ALWAYS, 0, nullptr);
	if (hFile == INVALID_HANDLE_VALUE) {
		return false;
	}
	DWORD cbWritten = 0;
	const BOOL bWritten = WriteFile(hFile, utf8, static_cast<DWORD>(cbUtf8 - 1), &cbWritten, nullptr);
	CloseHandle(hFile);
	return bWritten && cbWritten == static_cast<DWORD>(cbUtf8 - 1);
}

void CopyTextToClipboard(HWND hwnd, LPCWSTR pszText) noexcept {
	if (pszText == nullptr || pszText[0] == L'\0' || !OpenClipboard(hwnd)) {
		return;
	}
	EmptyClipboard();
	const size_t cbText = (lstrlen(pszText) + 1) * sizeof(WCHAR);
	HGLOBAL hMem = GlobalAlloc(GMEM_MOVEABLE, cbText);
	if (hMem != nullptr) {
		void *pMem = GlobalLock(hMem);
		if (pMem != nullptr) {
			memcpy(pMem, pszText, cbText);
			GlobalUnlock(hMem);
			SetClipboardData(CF_UNICODETEXT, hMem);
		} else {
			GlobalFree(hMem);
		}
	}
	CloseClipboard();
}

// ---------------------------------------------------------------------------
// 文件夹树
// ---------------------------------------------------------------------------

LPARAM GetTreeParam(HTREEITEM hItem) noexcept {
	TVITEM tvi;
	tvi.mask = TVIF_PARAM;
	tvi.hItem = hItem;
	TreeView_GetItem(hwndFolderTree, &tvi);
	return tvi.lParam;
}

// 插入占位子节点：展开时才真正枚举子目录。
// 占位节点的 lParam 为 0，真实节点在堆上保存绝对路径。
void AddTreeNodePlaceholder(HTREEITEM hParent) noexcept {
	TVINSERTSTRUCT tvis;
	tvis.hParent = hParent;
	tvis.hInsertAfter = TVI_LAST;
	tvis.item.mask = TVIF_TEXT | TVIF_PARAM;
	tvis.item.pszText = const_cast<LPWSTR>(L"");
	tvis.item.lParam = 0;
	TreeView_InsertItem(hwndFolderTree, &tvis);
}

HTREEITEM AddTreeNode(HTREEITEM hParent, LPCWSTR pszName, LPCWSTR pszPath, int iImage) noexcept {
	TVINSERTSTRUCT tvis;
	tvis.hParent = hParent;
	tvis.hInsertAfter = TVI_LAST;
	tvis.item.mask = TVIF_TEXT | TVIF_IMAGE | TVIF_SELECTEDIMAGE | TVIF_PARAM;
	tvis.item.pszText = const_cast<LPWSTR>(pszName);
	tvis.item.iImage = iImage;
	tvis.item.iSelectedImage = iImage;
	tvis.item.lParam = AsInteger<LPARAM>(HeapStrDupW(pszPath));
	const HTREEITEM hItem = TreeView_InsertItem(hwndFolderTree, &tvis);
	AddTreeNodePlaceholder(hItem);
	return hItem;
}

void FillTreeFolder(HTREEITEM hParent, LPCWSTR pszDir) noexcept {
	FolderEntry *pEntries = nullptr;
	const int count = CollectEntries(pszDir, true, &pEntries);
	if (pEntries == nullptr) {
		return;
	}
	const int iImage = IconIndexForEntry(true);
	for (int i = 0; i < count; i++) {
		WCHAR szChild[MAX_PATH];
		PathCombine(szChild, pszDir, pEntries[i].name);
		AddTreeNode(hParent, pEntries[i].name, szChild, iImage);
	}
	NP2HeapFree(pEntries);
}

void EnsureTreeChildren(HTREEITEM hItem) noexcept {
	// 子节点仍是占位节点（lParam == 0）时才需要填
	const HTREEITEM hChild = TreeView_GetChild(hwndFolderTree, hItem);
	if (hChild == nullptr || GetTreeParam(hChild) != 0) {
		return;
	}
	TreeView_DeleteItem(hwndFolderTree, hChild);
	const LPARAM param = GetTreeParam(hItem);
	if (param != 0) {
		FillTreeFolder(hItem, reinterpret_cast<LPCWSTR>(param));
	}
}

void AddTreeRoots() noexcept {
	const int iFolderIcon = IconIndexForEntry(true);

	const DWORD drives = GetLogicalDrives();
	WCHAR szRoot[] = L"A:\\";
	for (int i = 0; i < 26; i++) {
		if ((drives & (1U << i)) == 0) {
			continue;
		}
		szRoot[0] = static_cast<WCHAR>(L'A' + i);

		WCHAR szName[MAX_PATH];
		const UINT driveType = GetDriveType(szRoot);
		WCHAR szLabel[MAX_PATH] = L"";
		if ((driveType == DRIVE_REMOVABLE || driveType == DRIVE_FIXED) &&
			GetVolumeInformation(szRoot, szLabel, COUNTOF(szLabel), nullptr, nullptr, nullptr, nullptr, 0) && StrNotEmpty(szLabel)) {
			wsprintf(szName, L"%s (%s)", szLabel, szRoot);
		} else {
			lstrcpyn(szName, szRoot, COUNTOF(szName));
		}

		SHFILEINFO shfi;
		SHGetFileInfo2(szRoot, 0, &shfi, sizeof(SHFILEINFO), SHGFI_SMALLICON | SHGFI_SYSICONINDEX);
		AddTreeNode(TVI_ROOT, szName, szRoot, shfi.iIcon);
	}

	LPWSTR pszPath = nullptr;
	if (S_OK == SHGetKnownFolderPath(FOLDERID_Desktop, KF_FLAG_DEFAULT, nullptr, &pszPath)) {
		AddTreeNode(TVI_ROOT, L"Desktop", pszPath, iFolderIcon);
		CoTaskMemFree(pszPath);
	}
}

// ---------------------------------------------------------------------------
// 文件列表
// ---------------------------------------------------------------------------

void GetListItemText(int iItem, LPWSTR pszText, int cchText) noexcept {
	pszText[0] = L'\0';
	ListView_GetItemText(hwndFolderList, iItem, 0, pszText, cchText - 1);
}

void SetListSubItemText(int iItem, int iSubItem, LPCWSTR pszText) noexcept {
	ListView_SetItemText(hwndFolderList, iItem, iSubItem, const_cast<LPWSTR>(pszText));
}

void BeginEditComment(int iItem) noexcept;
void EndEditComment(bool bSave, bool bRestoreFocus) noexcept;

// 造列：Name / Size / Date modified / Attributes / Comment
void CreateColumns() noexcept {
	ListView_DeleteColumn(hwndFolderList, 0);
	const struct { LPCWSTR name; int width; } columns[FolderListColumn_Count] = {
		{ L"Name",			FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Name] },
		{ L"Size",			FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Size] },
		{ L"Date Modified",	FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Date] },
		{ L"Attributes",	FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Attributes] },
		{ L"Comment",		FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Comment] },
	};
	for (int i = 0; i < FolderListColumn_Count; i++) {
		LVCOLUMN lvc;
		lvc.mask = LVCF_FMT | LVCF_TEXT | LVCF_WIDTH;
		lvc.fmt = LVCFMT_LEFT;
		lvc.cx = ScaleForDpi(columns[i].width);
		lvc.pszText = const_cast<LPWSTR>(columns[i].name);
		ListView_InsertColumn(hwndFolderList, i, &lvc);
	}
}

// Name 列吃掉剩余宽度，其余列固定，面板拖宽时属性列始终可见
void ApplyColumnWidths(int panelWidth) noexcept {
	int cxFixed = 0;
	for (int i = 1; i < FolderListColumn_Count; i++) {
		cxFixed += ScaleForDpi(FOLDER_LIST_COLUMN_WIDTHS[i]);
	}
	const int widths[FolderListColumn_Count] = {
		max(panelWidth - cxFixed, ScaleForDpi(FOLDER_LIST_MIN_NAME_WIDTH)),
		ScaleForDpi(FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Size]),
		ScaleForDpi(FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Date]),
		ScaleForDpi(FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Attributes]),
		ScaleForDpi(FOLDER_LIST_COLUMN_WIDTHS[FolderListColumn_Comment]),
	};
	for (int i = 0; i < FolderListColumn_Count; i++) {
		if (ListView_GetColumnWidth(hwndFolderList, i) != widths[i]) {
			ListView_SetColumnWidth(hwndFolderList, i, widths[i]);
		}
	}
}

// 填一个条目的「大小 / 修改日期 / 属性 / 备注」
void SetListRowDetails(int iItem, LPCWSTR pszFullPath, DWORD dwAttributes, ULONGLONG size, const FILETIME &ft) noexcept {
	WCHAR szText[320];

	if ((dwAttributes & FILE_ATTRIBUTE_DIRECTORY) == 0) {
		FormatFileSize(size, szText, COUNTOF(szText));
		SetListSubItemText(iItem, FolderListColumn_Size, szText);
	}

	FormatFileTime(ft, szText, COUNTOF(szText));
	SetListSubItemText(iItem, FolderListColumn_Date, szText);

	FormatFileAttributes(dwAttributes, szText, COUNTOF(szText));
	SetListSubItemText(iItem, FolderListColumn_Attributes, szText);

	WCHAR szNote[FOLDER_BROWSER_NOTE_MAX + 1];
	if (ReadFileNote(pszFullPath, szNote, COUNTOF(szNote))) {
		SetListSubItemText(iItem, FolderListColumn_Comment, szNote);
	}
}

void FillList(LPCWSTR pszDir) noexcept {
	if (hwndFolderList == nullptr) {
		return;
	}
	// 换目录/刷新时正在编辑的备注先收尾
	EndEditComment(true, false);

	if (StrNotEmpty(pszDir) && PathIsDirectory(pszDir)) {
		lstrcpyn(szFolderDir, pszDir, COUNTOF(szFolderDir));
	}

	bFolderSyncing = true;
	ListView_DeleteAllItems(hwndFolderList);

	if (StrIsEmpty(szFolderDir)) {
		bFolderSyncing = false;
		return;
	}

	FolderEntry *pEntries = nullptr;
	const int count = CollectEntries(szFolderDir, false, &pEntries);

	int iItem = 0;
	if (!PathIsRoot(szFolderDir)) {
		LVITEM lvi;
		lvi.mask = LVIF_TEXT | LVIF_IMAGE;
		lvi.iItem = iItem;
		lvi.iSubItem = 0;
		lvi.pszText = const_cast<LPWSTR>(L"..");
		lvi.iImage = IconIndexForEntry(true);
		ListView_InsertItem(hwndFolderList, &lvi);
		SetListSubItemText(iItem, FolderListColumn_Attributes, L"D");
		++iItem;
	}
	if (pEntries != nullptr) {
		WCHAR szFull[MAX_PATH];
		for (int i = 0; i < count; i++) {
			LVITEM lvi;
			lvi.mask = LVIF_TEXT | LVIF_IMAGE;
			lvi.iItem = iItem;
			lvi.iSubItem = 0;
			lvi.pszText = pEntries[i].name;
			lvi.iImage = IconIndexForEntry(pEntries[i].isDir);
			ListView_InsertItem(hwndFolderList, &lvi);

			PathCombine(szFull, szFolderDir, pEntries[i].name);
			SetListRowDetails(iItem, szFull, pEntries[i].dwFileAttributes, pEntries[i].size, pEntries[i].ftLastWrite);
			++iItem;
		}
		NP2HeapFree(pEntries);
	}

	bFolderSyncing = false;
	ApplyColumnWidths(cxFolderPanel);
}

void SelectListItem(LPCWSTR pszName) noexcept {
	if (hwndFolderList == nullptr) {
		return;
	}
	WCHAR szText[MAX_PATH];
	const int count = ListView_GetItemCount(hwndFolderList);
	for (int i = 0; i < count; i++) {
		GetListItemText(i, szText, COUNTOF(szText));
		if (StrCmpLogicalW(szText, pszName) == 0) {
			ListView_SetItemState(hwndFolderList, i, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED);
			ListView_EnsureVisible(hwndFolderList, i, FALSE);
			break;
		}
	}
}

// 判断节点路径是否为目标目录路径的祖先（含自身）
bool IsPathAncestorOrSelf(LPCWSTR pszNode, LPCWSTR pszTarget) noexcept {
	WCHAR szNode[MAX_PATH];
	WCHAR szTarget[MAX_PATH];
	lstrcpyn(szNode, pszNode, COUNTOF(szNode));
	lstrcpyn(szTarget, pszTarget, COUNTOF(szTarget));
	PathRemoveBackslash(szNode);
	PathRemoveBackslash(szTarget);

	const int cchNode = lstrlen(szNode);
	if (cchNode == 0 || StrCmpNI(szNode, szTarget, cchNode) != 0) {
		return false;
	}
	return szTarget[cchNode] == L'\0' || szTarget[cchNode] == L'\\';
}

bool DescendTreeFolder(HTREEITEM hItem, LPCWSTR pszTarget) noexcept {
	EnsureTreeChildren(hItem);
	for (HTREEITEM hChild = TreeView_GetChild(hwndFolderTree, hItem);
		 hChild != nullptr; hChild = TreeView_GetNextSibling(hwndFolderTree, hChild)) {
		const LPARAM param = GetTreeParam(hChild);
		if (param == 0) {
			continue;
		}
		LPCWSTR const pszChild = reinterpret_cast<LPCWSTR>(param);
		if (!IsPathAncestorOrSelf(pszChild, pszTarget)) {
			continue;
		}
		if (StrCmpI(pszChild, pszTarget) == 0 || DescendTreeFolder(hChild, pszTarget)) {
			TreeView_SelectItem(hwndFolderTree, hChild);
			TreeView_EnsureVisible(hwndFolderTree, hChild);
			return true;
		}
	}
	return false;
}

// 在树里定位到指定目录：按需向下枚举子目录，不整盘扫描。
void SelectTreeFolder(LPCWSTR pszDir) noexcept {
	if (hwndFolderTree == nullptr || StrIsEmpty(pszDir)) {
		return;
	}
	for (HTREEITEM hRoot = TreeView_GetRoot(hwndFolderTree);
		 hRoot != nullptr; hRoot = TreeView_GetNextSibling(hwndFolderTree, hRoot)) {
		const LPARAM param = GetTreeParam(hRoot);
		if (param == 0) {
			continue;
		}
		const LPCWSTR pszRoot = reinterpret_cast<LPCWSTR>(param);
		if (IsPathAncestorOrSelf(pszRoot, pszDir) && DescendTreeFolder(hRoot, pszDir)) {
			return;
		}
	}
}

// ---------------------------------------------------------------------------
// 用户操作
// ---------------------------------------------------------------------------

void OpenListEntry(int iItem) noexcept {
	WCHAR szName[MAX_PATH];
	GetListItemText(iItem, szName, COUNTOF(szName));
	if (StrIsEmpty(szName)) {
		return;
	}

	if (StrEqual(szName, L"..")) {
		WCHAR szUp[MAX_PATH];
		lstrcpyn(szUp, szFolderDir, COUNTOF(szUp));
		PathRemoveFileSpec(szUp);
		if (StrNotEmpty(szUp) && PathIsDirectory(szUp)) {
			FillList(szUp);
		}
		return;
	}

	WCHAR szFull[MAX_PATH];
	PathCombine(szFull, szFolderDir, szName);
	const DWORD dwAttributes = GetFileAttributes(szFull);
	if (dwAttributes == INVALID_FILE_ATTRIBUTES) {
		return;
	}
	if ((dwAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) {
		FillList(szFull);
		return;
	}

	// 单击即切换：直接在当前窗口打开，不新开窗口
	if (StrNotEmpty(szCurFile) && PathEqual(szFull, szCurFile)) {
		return;
	}
	FileLoad(FileLoadFlag_Default, szFull);
}

void NavigateToTreeItem(HTREEITEM hItem) noexcept {
	const LPARAM param = GetTreeParam(hItem);
	if (param != 0) {
		FillList(reinterpret_cast<LPCWSTR>(param));
	}
}

// 取列表项对应的完整路径；「..」或空项返回 false
bool GetListItemPath(int iItem, LPWSTR pszPath, int cchPath, bool *pbIsDir) noexcept {
	WCHAR szName[MAX_PATH];
	GetListItemText(iItem, szName, COUNTOF(szName));
	if (StrIsEmpty(szName) || StrEqual(szName, L"..")) {
		return false;
	}
	if (lstrlen(szFolderDir) + lstrlen(szName) + 2 > cchPath) {
		return false;
	}
	PathCombine(pszPath, szFolderDir, szName);
	if (pbIsDir != nullptr) {
		const DWORD dwAttributes = GetFileAttributes(pszPath);
		*pbIsDir = dwAttributes != INVALID_FILE_ATTRIBUTES && (dwAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0;
	}
	return true;
}

int GetSelectedListItem() noexcept {
	return ListView_GetNextItem(hwndFolderList, -1, LVNI_SELECTED);
}

// ---------------------------------------------------------------------------
// 备注内联编辑
// ---------------------------------------------------------------------------

LRESULT CALLBACK NoteEditSubclassProc(HWND hwnd, UINT umsg, WPARAM wParam, LPARAM lParam,
									  UINT_PTR uIdSubclass, DWORD_PTR dwRefData) noexcept {
	UNREFERENCED_PARAMETER(dwRefData);

	switch (umsg) {
	case WM_KEYDOWN:
		if (wParam == VK_RETURN) {
			PostMessage(hwndMain, APPM_FOLDERBROWSER_NOTE, 1, 0);
			return 0;
		}
		if (wParam == VK_ESCAPE) {
			PostMessage(hwndMain, APPM_FOLDERBROWSER_NOTE, 0, 0);
			return 0;
		}
		break;

	case WM_KILLFOCUS:
		// 点到别处也算确认
		PostMessage(hwndMain, APPM_FOLDERBROWSER_NOTE, 2, 0);
		break;

	case WM_NCDESTROY:
		RemoveWindowSubclass(hwnd, NoteEditSubclassProc, uIdSubclass);
		break;
	}
	return DefSubclassProc(hwnd, umsg, wParam, lParam);
}

void EnsureNoteEdit() noexcept {
	if (hwndNoteEdit != nullptr) {
		return;
	}
	hwndNoteEdit = CreateWindowEx(WS_EX_CLIENTEDGE, WC_EDIT, L"", WS_CHILD | ES_AUTOHSCROLL,
								  0, 0, 0, 0, hwndFolderList,
								  AsPointer<HMENU, ULONG_PTR>(IDC_FOLDERBROWSER_NOTEEDIT), nullptr, nullptr);
	if (hwndNoteEdit != nullptr) {
		SendMessage(hwndNoteEdit, WM_SETFONT, AsInteger<WPARAM>(hFontFolder), TRUE);
		SendMessage(hwndNoteEdit, EM_SETLIMITTEXT, FOLDER_BROWSER_NOTE_MAX, 0);
		SetWindowSubclass(hwndNoteEdit, NoteEditSubclassProc, 0, 0);
	}
}

void BeginEditComment(int iItem) noexcept {
	if (hwndFolderList == nullptr || iItem < 0) {
		return;
	}
	bool bIsDir = false;
	if (!GetListItemPath(iItem, szNoteEditPath, COUNTOF(szNoteEditPath), &bIsDir) || bIsDir) {
		return;	// 目录和「..」不支持备注
	}
	EndEditComment(true, false);
	EnsureNoteEdit();
	if (hwndNoteEdit == nullptr) {
		return;
	}

	RECT rc;
	if (!ListView_GetSubItemRect(hwndFolderList, iItem, FolderListColumn_Comment, LVIR_BOUNDS, &rc)) {
		return;
	}
	// 先把原有备注填进去再显示，避免出现一瞬间的空白
	WCHAR szNote[FOLDER_BROWSER_NOTE_MAX + 1];
	szNote[0] = L'\0';
	if (!ReadFileNote(szNoteEditPath, szNote, COUNTOF(szNote))) {
		szNote[0] = L'\0';
	}
	SetWindowText(hwndNoteEdit, szNote);
	SendMessage(hwndNoteEdit, EM_SETSEL, 0, -1);
	iNoteEditItem = iItem;

	const int cyMin = ScaleForDpi(18);
	const int cy = max(static_cast<int>(rc.bottom - rc.top), cyMin);
	SetWindowPos(hwndNoteEdit, HWND_TOP, rc.left, rc.top, rc.right - rc.left, cy, SWP_SHOWWINDOW);
	SetFocus(hwndNoteEdit);
}

void EndEditComment(bool bSave, bool bRestoreFocus) noexcept {
	if (hwndNoteEdit == nullptr || iNoteEditItem < 0 || bNoteEditEnding) {
		return;
	}

	bNoteEditEnding = true;
	const int iItem = iNoteEditItem;
	WCHAR szNote[FOLDER_BROWSER_NOTE_MAX + 1];
	szNote[0] = L'\0';
	if (bSave) {
		GetWindowText(hwndNoteEdit, szNote, COUNTOF(szNote));
		StrTrim(szNote, L" \t\r\n");
	}
	iNoteEditItem = -1;
	ShowWindow(hwndNoteEdit, SW_HIDE);

	if (bSave) {
		if (WriteFileNote(szNoteEditPath, szNote)) {
			SetListSubItemText(iItem, FolderListColumn_Comment, szNote);
		} else {
			MessageBoxW(hwndMain,
						L"Failed to save the comment for this file.\n"
						L"Comments are stored in an NTFS alternate data stream, "
						L"so they need NTFS and write permission on the file.",
						L"Notepad4", MB_OK | MB_ICONEXCLAMATION);
		}
	}

	bNoteEditEnding = false;
	if (bRestoreFocus) {
		SetFocus(hwndFolderList);
	}
}

// ---------------------------------------------------------------------------
// 右键菜单 / 复制
// ---------------------------------------------------------------------------

void CopyListItemText(int iItem, bool bFullPath) noexcept {
	WCHAR szPath[MAX_PATH];
	if (!GetListItemPath(iItem, szPath, COUNTOF(szPath), nullptr)) {
		return;
	}
	CopyTextToClipboard(hwndMain, bFullPath ? szPath : PathFindFileName(szPath));
}

void ExecuteBrowseCommand(int cmd, int iItem) noexcept {
	switch (cmd) {
	case FolderBrowseCommand_CopyName:
		CopyListItemText(iItem, false);
		break;

	case FolderBrowseCommand_CopyPath:
		CopyListItemText(iItem, true);
		break;

	case FolderBrowseCommand_EditComment:
		if (iItem >= 0) {
			BeginEditComment(iItem);
		}
		break;

	case FolderBrowseCommand_ClearComment: {
		if (iItem < 0) {
			break;
		}
		bool bIsDir = false;
		WCHAR szPath[MAX_PATH];
		if (!GetListItemPath(iItem, szPath, COUNTOF(szPath), &bIsDir) || bIsDir) {
			break;
		}
		if (WriteFileNote(szPath, L"")) {
			SetListSubItemText(iItem, FolderListColumn_Comment, L"");
		}
	}
	break;

	case FolderBrowseCommand_Refresh:
		FillList(szFolderDir);
		break;
	}
}

void ShowListContextMenu(int iItem) noexcept {
	HMENU hMenu = CreatePopupMenu();
	if (hMenu == nullptr) {
		return;
	}

	bool bIsDir = false;
	WCHAR szPath[MAX_PATH];
	const bool bHasItem = (iItem >= 0) && GetListItemPath(iItem, szPath, COUNTOF(szPath), &bIsDir);

	AppendMenu(hMenu, MF_STRING, FolderBrowseCommand_CopyName, L"Copy Name\tCtrl+C");
	AppendMenu(hMenu, MF_STRING, FolderBrowseCommand_CopyPath, L"Copy Full Path\tCtrl+Shift+C");
	AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
	AppendMenu(hMenu, MF_STRING, FolderBrowseCommand_EditComment, L"Edit Comment");
	AppendMenu(hMenu, MF_STRING, FolderBrowseCommand_ClearComment, L"Clear Comment");
	AppendMenu(hMenu, MF_SEPARATOR, 0, nullptr);
	AppendMenu(hMenu, MF_STRING, FolderBrowseCommand_Refresh, L"Refresh");

	if (!bHasItem) {
		EnableMenuItem(hMenu, FolderBrowseCommand_CopyName, MF_BYCOMMAND | MF_GRAYED);
		EnableMenuItem(hMenu, FolderBrowseCommand_CopyPath, MF_BYCOMMAND | MF_GRAYED);
	}
	if (!bHasItem || bIsDir) {
		EnableMenuItem(hMenu, FolderBrowseCommand_EditComment, MF_BYCOMMAND | MF_GRAYED);
		EnableMenuItem(hMenu, FolderBrowseCommand_ClearComment, MF_BYCOMMAND | MF_GRAYED);
	}

	POINT pt;
	GetCursorPos(&pt);
	SetForegroundWindow(hwndMain);
	const int cmd = TrackPopupMenu(hMenu, TPM_RETURNCMD | TPM_RIGHTBUTTON, pt.x, pt.y, 0, hwndMain, nullptr);
	DestroyMenu(hMenu);
	if (cmd != 0) {
		ExecuteBrowseCommand(cmd, iItem);
	}
}

// ---------------------------------------------------------------------------
// 分隔条
// ---------------------------------------------------------------------------

void ApplyPanelWidth(int width) noexcept {
	RECT rc;
	GetClientRect(hwndMain, &rc);
	const int cxMin = ScaleForDpi(FOLDER_BROWSER_MIN_WIDTH);
	const int cxMax = max(cxMin, static_cast<int>(rc.right) - ScaleForDpi(FOLDER_BROWSER_MIN_EDIT_WIDTH));
	cxFolderPanel = clamp(width, cxMin, cxMax);

	const UINT dpi = GetWindowDPI(hwndMain);
	if (dpi != 0) {
		iFolderBrowserWidth = MulDiv(cxFolderPanel, USER_DEFAULT_SCREEN_DPI, static_cast<int>(dpi));
	}
}

LRESULT CALLBACK FolderSplitterProc(HWND hwnd, UINT umsg, WPARAM wParam, LPARAM lParam) {
	UNREFERENCED_PARAMETER(wParam);

	switch (umsg) {
	case WM_SETCURSOR:
		SetCursor(LoadCursor(nullptr, (hwnd == hwndSplitterV) ? IDC_SIZEWE : IDC_SIZENS));
		return TRUE;

	case WM_LBUTTONDOWN: {
		const bool bVertical = (hwnd == hwndSplitterV);
		POINT pt;
		pt.x = GET_X_LPARAM(lParam);
		pt.y = GET_Y_LPARAM(lParam);
		ClientToScreen(hwnd, &pt);
		iDragOrigin = static_cast<int>(bVertical ? pt.x : pt.y);
		iDragStartValue = bVertical ? cxFolderPanel : cyFolderTree;
		bDraggingV = bVertical;
		bDraggingH = !bVertical;
		SetCapture(hwnd);
	}
	break;

	case WM_MOUSEMOVE:
		if (bDraggingV || bDraggingH) {
			POINT pt;
			pt.x = GET_X_LPARAM(lParam);
			pt.y = GET_Y_LPARAM(lParam);
			ClientToScreen(hwnd, &pt);
			if (bDraggingV) {
				ApplyPanelWidth(iDragStartValue + (static_cast<int>(pt.x) - iDragOrigin));
			} else {
				cyFolderTree = max(iDragStartValue + (static_cast<int>(pt.y) - iDragOrigin),
								   ScaleForDpi(FOLDER_BROWSER_MIN_TREE_HEIGHT));
			}
			SendWMSize(hwndMain);
		}
		break;

	case WM_LBUTTONUP:
		if (bDraggingV || bDraggingH) {
			bDraggingV = false;
			bDraggingH = false;
			ReleaseCapture();
			IniSetIntEx(INI_SECTION_NAME_SETTINGS, L"FolderBrowserWidth", iFolderBrowserWidth, FOLDER_BROWSER_DEFAULT_WIDTH);
		}
		break;

	case WM_CAPTURECHANGED:
		bDraggingV = false;
		bDraggingH = false;
		break;
	}
	return DefWindowProc(hwnd, umsg, wParam, lParam);
}

void RegisterSplitterClass(HINSTANCE hInstance) noexcept {
	WNDCLASSEX wc;
	if (GetClassInfoEx(hInstance, WC_FOLDER_BROWSER_SPLITTER, &wc)) {
		return;
	}
	wc.cbSize = sizeof(WNDCLASSEX);
	wc.style = 0;
	wc.lpfnWndProc = FolderSplitterProc;
	wc.cbClsExtra = 0;
	wc.cbWndExtra = 0;
	wc.hInstance = hInstance;
	wc.hIcon = nullptr;
	wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
	wc.hbrBackground = AsPointer<HBRUSH, ULONG_PTR>(COLOR_3DFACE + 1);
	wc.lpszMenuName = nullptr;
	wc.lpszClassName = WC_FOLDER_BROWSER_SPLITTER;
	wc.hIconSm = nullptr;
	RegisterClassEx(&wc);
}

void CreatePanelFont(HWND hwnd) noexcept {
	NONCLIENTMETRICS ncm;
	ncm.cbSize = sizeof(ncm);
	if (!SystemParametersInfo(SPI_GETNONCLIENTMETRICS, sizeof(ncm), &ncm, 0)) {
		return;
	}
	LOGFONT lf = ncm.lfMessageFont;
	const UINT dpi = GetWindowDPI(hwnd);
	if (g_uSystemDPI != 0 && dpi != g_uSystemDPI) {
		lf.lfHeight = MulDiv(lf.lfHeight, static_cast<int>(dpi), static_cast<int>(g_uSystemDPI));
	}
	if (hFontFolder != nullptr) {
		DeleteObject(hFontFolder);
	}
	hFontFolder = CreateFontIndirect(&lf);
	if (hFontFolder != nullptr) {
		SendMessage(hwndFolderTree, WM_SETFONT, AsInteger<WPARAM>(hFontFolder), TRUE);
		SendMessage(hwndFolderList, WM_SETFONT, AsInteger<WPARAM>(hFontFolder), TRUE);
	}
}

void ApplyPanelVisible(bool bShow) noexcept {
	const int cmd = bShow ? SW_SHOW : SW_HIDE;
	ShowWindow(hwndFolderTree, cmd);
	ShowWindow(hwndFolderList, cmd);
	ShowWindow(hwndSplitterH, cmd);
	ShowWindow(hwndSplitterV, cmd);
}

} // namespace

//=============================================================================
//
// FolderBrowser API
//
//=============================================================================

void FolderBrowser_Create(HWND hwnd, HINSTANCE hInstance) noexcept {
	RegisterSplitterClass(hInstance);

	hwndFolderTree = CreateWindowEx(0, WC_TREEVIEW, nullptr,
									WS_CHILD | WS_TABSTOP | TVS_HASBUTTONS | TVS_HASLINES | TVS_LINESATROOT | TVS_SHOWSELALWAYS,
									0, 0, 0, 0, hwnd, AsPointer<HMENU, ULONG_PTR>(IDC_FOLDERBROWSER_TREE), hInstance, nullptr);
	DarkMode_InitTreeView(hwndFolderTree);

	hwndFolderList = CreateWindowEx(0, WC_LISTVIEW, nullptr,
									WS_CHILD | WS_TABSTOP | LVS_REPORT | LVS_SINGLESEL | LVS_SHOWSELALWAYS | LVS_NOSORTHEADER,
									0, 0, 0, 0, hwnd, AsPointer<HMENU, ULONG_PTR>(IDC_FOLDERBROWSER_LIST), hInstance, nullptr);
	DarkMode_InitFileListView(hwndFolderList);
	ListView_SetExtendedListViewStyle(hwndFolderList,
									  ListView_GetExtendedListViewStyle(hwndFolderList) | LVS_EX_FULLROWSELECT);
	CreateColumns();

	hwndSplitterH = CreateWindowEx(0, WC_FOLDER_BROWSER_SPLITTER, nullptr, WS_CHILD,
								   0, 0, 0, 0, hwnd, AsPointer<HMENU, ULONG_PTR>(IDC_FOLDERBROWSER_SPLITTER_H), hInstance, nullptr);
	hwndSplitterV = CreateWindowEx(0, WC_FOLDER_BROWSER_SPLITTER, nullptr, WS_CHILD,
								   0, 0, 0, 0, hwnd, AsPointer<HMENU, ULONG_PTR>(IDC_FOLDERBROWSER_SPLITTER_V), hInstance, nullptr);

	// 系统提供的文件夹图标列表（共享资源，不要销毁）
	SHFILEINFO shfi;
	himlFolder = AsPointer<HIMAGELIST>(SHGetFileInfo2(L"C:\\", 0, &shfi, sizeof(SHFILEINFO),
													  SHGFI_SMALLICON | SHGFI_SYSICONINDEX));
	if (himlFolder != nullptr) {
		TreeView_SetImageList(hwndFolderTree, himlFolder, TVSIL_NORMAL);
		ListView_SetImageList(hwndFolderList, himlFolder, LVSIL_SMALL);
	}

	CreatePanelFont(hwnd);
	AddTreeRoots();
	ApplyPanelVisible(bShowFolderBrowser);

	// 初次显示时跟随当前文件（启动时可能已经打开了文件）
	if (StrNotEmpty(szCurFile)) {
		FolderBrowser_SyncToFile(szCurFile);
	}
}

void FolderBrowser_Destroy() noexcept {
	bDraggingV = false;
	bDraggingH = false;
	iNoteEditItem = -1;
	if (hwndNoteEdit != nullptr) {
		DestroyWindow(hwndNoteEdit);
		hwndNoteEdit = nullptr;
	}
	if (hFontFolder != nullptr) {
		DeleteObject(hFontFolder);
		hFontFolder = nullptr;
	}
	// 先销毁子窗口，让 TVN_DELETEITEM 有机会回收树节点上的路径字符串
	if (hwndSplitterV != nullptr) {
		DestroyWindow(hwndSplitterV);
	}
	if (hwndSplitterH != nullptr) {
		DestroyWindow(hwndSplitterH);
	}
	if (hwndFolderTree != nullptr) {
		DestroyWindow(hwndFolderTree);
	}
	if (hwndFolderList != nullptr) {
		DestroyWindow(hwndFolderList);
	}
	hwndFolderTree = nullptr;
	hwndFolderList = nullptr;
	hwndSplitterV = nullptr;
	hwndSplitterH = nullptr;
	himlFolder = nullptr;
}

void FolderBrowser_SetVisible(HWND hwnd, bool bShow) noexcept {
	bShowFolderBrowser = bShow;
	if (hwndFolderTree == nullptr) {
		return;
	}
	ApplyPanelVisible(bShow);
	if (bShow && StrIsEmpty(szFolderDir) && StrNotEmpty(szCurFile)) {
		FolderBrowser_SyncToFile(szCurFile);
	}
	SendWMSize(hwnd);
}

void FolderBrowser_Toggle(HWND hwnd) noexcept {
	FolderBrowser_SetVisible(hwnd, !bShowFolderBrowser);
}

int FolderBrowser_GetWidth() noexcept {
	if (!bShowFolderBrowser || hwndFolderTree == nullptr) {
		return 0;
	}
	if (cxFolderPanel <= 0) {
		ApplyPanelWidth(ScaleForDpi(iFolderBrowserWidth > 0 ? iFolderBrowserWidth : FOLDER_BROWSER_DEFAULT_WIDTH));
	}
	return cxFolderPanel + ScaleForDpi(FOLDER_BROWSER_SPLITTER_THICKNESS);
}

void FolderBrowser_Layout(int x, int y, int cx, int cy) noexcept {
	if (hwndFolderTree == nullptr || !bShowFolderBrowser) {
		return;
	}
	if (cxFolderPanel <= 0) {
		ApplyPanelWidth(ScaleForDpi(iFolderBrowserWidth > 0 ? iFolderBrowserWidth : FOLDER_BROWSER_DEFAULT_WIDTH));
	}

	const int thickness = ScaleForDpi(FOLDER_BROWSER_SPLITTER_THICKNESS);
	// 保证编辑区至少还能放下 FOLDER_BROWSER_MIN_EDIT_WIDTH
	const int panelWidth = min(cxFolderPanel,
							   max(ScaleForDpi(FOLDER_BROWSER_MIN_WIDTH), cx - thickness - ScaleForDpi(FOLDER_BROWSER_MIN_EDIT_WIDTH)));

	int treeHeight = cyFolderTree;
	if (treeHeight <= 0) {
		treeHeight = ScaleForDpi(FOLDER_BROWSER_DEFAULT_TREE_HEIGHT);
	}
	const int cyMinTree = ScaleForDpi(FOLDER_BROWSER_MIN_TREE_HEIGHT);
	const int cyMinList = ScaleForDpi(FOLDER_BROWSER_MIN_LIST_HEIGHT);
	treeHeight = clamp(treeHeight, cyMinTree, max(cyMinTree, cy - thickness - cyMinList));
	cyFolderTree = treeHeight;

	SetWindowPos(hwndFolderTree, nullptr, x, y, panelWidth, treeHeight, SWP_NOZORDER | SWP_NOACTIVATE);
	SetWindowPos(hwndSplitterH, nullptr, x, y + treeHeight, panelWidth, thickness, SWP_NOZORDER | SWP_NOACTIVATE);
	const int listY = y + treeHeight + thickness;
	SetWindowPos(hwndFolderList, nullptr, x, listY, panelWidth, max(cy - (listY - y), 0), SWP_NOZORDER | SWP_NOACTIVATE);
	SetWindowPos(hwndSplitterV, nullptr, x + panelWidth, y, thickness, cy, SWP_NOZORDER | SWP_NOACTIVATE);

	ApplyColumnWidths(panelWidth);
}

void FolderBrowser_OnDpiChanged() noexcept {
	if (hwndFolderTree == nullptr) {
		return;
	}
	cxFolderPanel = 0;	// 让下一次布局按新 DPI 重新换算
	cyFolderTree = 0;
	CreatePanelFont(hwndMain);
	SendWMSize(hwndMain);
}

bool FolderBrowser_HandleNotify(HWND hwnd, LPARAM lParam) noexcept {
	if (hwndFolderTree == nullptr) {
		return false;
	}
	UNREFERENCED_PARAMETER(hwnd);

	LPNMHDR const pnmh = AsPointer<LPNMHDR>(lParam);
	if (pnmh->hwndFrom != hwndFolderTree && pnmh->hwndFrom != hwndFolderList) {
		return false;
	}

	if (pnmh->hwndFrom == hwndFolderTree) {
		LPNMTREEVIEW const pnmv = AsPointer<LPNMTREEVIEW>(lParam);
		switch (pnmh->code) {
		case TVN_ITEMEXPANDING:
			if (pnmv->action == TVE_EXPAND) {
				EnsureTreeChildren(pnmv->itemNew.hItem);
			}
			break;

		case TVN_SELCHANGED:
			if (!bFolderSyncing) {
				NavigateToTreeItem(pnmv->itemNew.hItem);
			}
			break;

		case TVN_DELETEITEM:
			if (pnmv->itemOld.lParam != 0) {
				NP2HeapFree(reinterpret_cast<LPVOID>(pnmv->itemOld.lParam));
			}
			break;

		default:
			return false;
		}
		return true;
	}

	// 文件列表
	switch (pnmh->code) {
	case NM_CLICK:
	case NM_DBLCLK: {
		LPNMITEMACTIVATE const pnmia = AsPointer<LPNMITEMACTIVATE>(lParam);
		int iItem = pnmia->iItem;
		int iSubItem = pnmia->iSubItem;
		if (iItem < 0) {
			iItem = GetSelectedListItem();
		}
		if (iItem < 0 || bFolderSyncing) {
			break;
		}
		if (iSubItem < 0) {
			// 有些壳子不给 NM_CLICK 填 iSubItem，按光标位置再判一次
			LVHITTESTINFO hti{};
			GetCursorPos(&hti.pt);
			ScreenToClient(hwndFolderList, &hti.pt);
			ListView_SubItemHitTest(hwndFolderList, &hti);
			iSubItem = hti.iSubItem;
		}
		// 备注列是「交互区」：单击只选中（否则双击备注就变成先打开文件再编辑了）
		if (iSubItem == FolderListColumn_Comment) {
			if (pnmh->code == NM_DBLCLK) {
				// 延后一步再开编辑框：双击后面的那次鼠标抬起会让列表抢回焦点，
				// 立刻开编辑框会被 WM_KILLFOCUS 当场提交掉（表现为闪一下）。
				PostMessage(hwndMain, APPM_FOLDERBROWSER_NOTE, 3, iItem);
			}
			break;
		}
		OpenListEntry(iItem);
	}
	break;

	case NM_RCLICK: {
		LPNMITEMACTIVATE const pnmia = AsPointer<LPNMITEMACTIVATE>(lParam);
		int iItem = pnmia->iItem;
		if (iItem < 0) {
			// 合成消息/无命中时按光标位置再判一次
			LVHITTESTINFO hti{};
			GetCursorPos(&hti.pt);
			ScreenToClient(hwndFolderList, &hti.pt);
			ListView_SubItemHitTest(hwndFolderList, &hti);
			iItem = hti.iItem;
		}
		if (iItem >= 0) {
			ListView_SetItemState(hwndFolderList, iItem, LVIS_SELECTED | LVIS_FOCUSED, LVIS_SELECTED | LVIS_FOCUSED);
		}
		ShowListContextMenu(iItem);
	}
	break;

	case LVN_KEYDOWN: {
		// 注意：Ctrl+C / Ctrl+Shift+C / Ctrl+R 都被主窗口加速键表接走了，
		// 走的是 FolderBrowser_CopyFromList() / FolderBrowser_Refresh()。
		LPNMLVKEYDOWN const pnkd = AsPointer<LPNMLVKEYDOWN>(lParam);
		if (pnkd->wVKey == VK_RETURN) {
			const int iItem = GetSelectedListItem();
			if (iItem >= 0 && !bFolderSyncing) {
				OpenListEntry(iItem);
			}
		}
	}
	break;

	default:
		return false;
	}
	return true;
}

void FolderBrowser_SyncToFile(LPCWSTR pszFile) noexcept {
	if (hwndFolderTree == nullptr || StrIsEmpty(pszFile)) {
		return;
	}

	WCHAR szDir[MAX_PATH];
	lstrcpyn(szDir, pszFile, COUNTOF(szDir));
	PathRemoveFileSpec(szDir);
	if (StrIsEmpty(szDir) || !PathIsDirectory(szDir)) {
		return;
	}
	if (!bShowFolderBrowser) {
		// 面板没显示，等它显示时再枚举，省掉无谓的磁盘扫描
		SetStrEmpty(szFolderDir);
		return;
	}
	if (StrCmpI(szDir, szFolderDir) == 0) {
		SelectListItem(PathFindFileName(pszFile));
		return;
	}

	bFolderSyncing = true;
	FillList(szDir);
	SelectTreeFolder(szDir);
	SelectListItem(PathFindFileName(pszFile));
	bFolderSyncing = false;
}

// 备注内联编辑的收尾/开启。EDIT 的子类过程与列表的通知把动作投递到这里，
// 避免在自己的窗口过程里销毁自己，也避免焦点被后续鼠标消息抢走。
// action: 0 = 取消，1 = 确认并回到列表焦点，2 = 确认（失焦路径），3 = 在 lParam 指定的条目上开始编辑
void FolderBrowser_EndNoteEdit(WPARAM action, LPARAM lParam) noexcept {
	if (action == 3) {
		BeginEditComment(static_cast<int>(lParam));
		return;
	}
	if (hwndNoteEdit == nullptr || iNoteEditItem < 0) {
		return;
	}
	if (action == 0) {
		EndEditComment(false, true);
	} else {
		EndEditComment(true, action == 1);
	}
}

bool FolderBrowser_CopyFromList(bool bFullPath) noexcept {
	if (hwndFolderList == nullptr || !bShowFolderBrowser) {
		return false;
	}
	const HWND hwndFocus = GetFocus();
	// 正在编辑备注时，Ctrl+C 应该是复制编辑框里的文字
	if (hwndNoteEdit != nullptr && hwndFocus == hwndNoteEdit) {
		SendMessage(hwndNoteEdit, WM_COPY, 0, 0);
		return true;
	}
	if (hwndFocus != hwndFolderList) {
		return false;
	}
	const int iItem = GetSelectedListItem();
	if (iItem < 0) {
		return false;
	}
	CopyListItemText(iItem, bFullPath);
	return true;
}

void FolderBrowser_Refresh() noexcept {
	if (hwndFolderList != nullptr && bShowFolderBrowser && StrNotEmpty(szFolderDir)) {
		FillList(szFolderDir);
	}
}

