import { Routes } from '@angular/router';
import { AgentsPage } from './agents.page';
import { ChatPage } from './chat.page';
import { ToolsPage } from './tools.page';
import { EventsPage } from './events.page';
import { SystemPage } from './system.page';
import { ModelsPage } from './models.page';
import { MemoryPage } from './memory.page';
import { SettingsLayout } from './settings';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  { path: 'chat', component: ChatPage },
  { path: 'chat/:id', component: ChatPage },
  {
    path: 'settings', component: SettingsLayout, children: [
      { path: '', pathMatch: 'full', redirectTo: 'agents' },
      { path: 'agents', component: AgentsPage },
      { path: 'models', component: ModelsPage },
      { path: 'memory', component: MemoryPage },
      { path: 'tools', component: ToolsPage },
      { path: 'events', component: EventsPage },
      { path: 'system', component: SystemPage },
    ],
  },
  // Old addresses (bookmarks) from before Settings existed.
  ...['agents', 'models', 'memory', 'tools', 'events', 'system'].map(p => ({ path: p, redirectTo: `settings/${p}` })),
  { path: '**', redirectTo: 'chat' },
];
