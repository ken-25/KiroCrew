/** The session menu's move-to submenu lists chat folders in the sidebar's folder
 *  order (`dashboard.folder_sort`). When the settings read behind that order
 *  fails, the submenu is drawn in the stored order -- a different list than the
 *  one the person chose -- and the menu itself says NOTHING about it: the sidebar
 *  this menu opens from says the failure once, over the tree it draws, and one
 *  screen names a failure once. This file pins that the menu stays quiet.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import {
  installSoftNavigate,
  __resetErrorJournalForTests,
  __resetNavSeamForTests,
} from '../utils/errorReport'
import type { ChatFolder, ChatSlot } from '../types'
import SessionActionsMenu from '../components/SessionActionsMenu'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuTrigger,
} from '../components/ui/context-menu'

const mocks = vi.hoisted(() => ({
  chatFolders: vi.fn(),
  kirocrewConfig: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (target, property: string) => (
      property in target ? target[property] : vi.fn().mockResolvedValue([])
    ),
  }),
}))

// The submenu itself is a Radix Sub, flaky under jsdom; its presence is what
// this file gates on, so a stub that renders a marker is enough.
vi.mock('../components/FolderMoveSubmenu', () => ({ default: () => <div data-testid="move-submenu" /> }))
vi.mock('../components/SendToInstanceSubmenu', () => ({ default: () => null }))
vi.mock('../components/SessionColorSwatches', () => ({ default: () => null }))
vi.mock('../components/LinkedSurfacesSection', () => ({ default: () => null }))
vi.mock('../components/ExportSessionItem', () => ({ default: () => null }))
vi.mock('../components/ImportSessionItem', () => ({ default: () => null }))
vi.mock('../hooks/useSessionActions', () => ({
  useSessionActions: () => ({
    toggleRead: vi.fn(),
    togglePin: vi.fn(),
    toggleMode: vi.fn(),
    copyLink: vi.fn(),
    move: vi.fn(),
    reload: vi.fn(),
    close: vi.fn(),
  }),
}))
vi.mock('../hooks/useChatPopouts', () => ({
  useChatPopouts: () => ({
    isPoppedOut: () => false,
    isSelfPopout: () => false,
    open: vi.fn(),
    focus: vi.fn(),
    bringBack: vi.fn(),
    returnSelfToMain: vi.fn(),
  }),
}))
vi.mock('../hooks/useTagPopover', () => ({
  useTagPopover: () => ({ open: vi.fn() }),
}))

const FOLDERS: ChatFolder[] = [{ id: 'work', name: 'Work', order: 0 }]

function mount() {
  const store = createTestStore()
  store.dispatch(sseSlots([{
    key: 'context-slot',
    messages: 1,
    running: false,
    memory_mode: 'persistent',
  } as ChatSlot]))
  const view = renderWithProviders(
    <ContextMenu>
      <ContextMenuTrigger asChild>
        <button type="button" data-testid="context-trigger">Actions</button>
      </ContextMenuTrigger>
      <ContextMenuContent>
        <SessionActionsMenu variant="context" slotKey="context-slot" />
      </ContextMenuContent>
    </ContextMenu>,
    { store },
  )
  fireEvent.contextMenu(screen.getByTestId('context-trigger'))
  return view
}

beforeEach(() => {
  vi.clearAllMocks()
  mocks.chatFolders.mockResolvedValue(FOLDERS)
  mocks.kirocrewConfig.mockResolvedValue({ dashboard: { folder_sort: 'name' } })
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  sessionStorage.clear()
  installSoftNavigate(() => {})
})

afterEach(() => {
  __resetNavSeamForTests()
  vi.restoreAllMocks()
})

describe('SessionActionsMenu folder-order read failure', () => {
  it('draws the submenu and says nothing itself, whether the order reads fine or not', async () => {
    mount()
    await screen.findByTestId('move-submenu')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /^ask the agent$/i })).toBeNull()
  })

  it('stays quiet on a failed read too: the sidebar it opens from says it once', async () => {
    mocks.kirocrewConfig.mockRejectedValue(new Error('gateway restarting'))
    mount()
    await screen.findByTestId('move-submenu')
    // Long enough for the failed read to settle into the menu's tree.
    await screen.findByRole('menuitem', { name: /tags/i })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByText('gateway restarting')).toBeNull()
    expect(screen.queryByRole('menuitem', { name: /^ask the agent$/i })).toBeNull()
  })
})
