import { Routes } from '@angular/router';
import { AgentsPage } from './agents.page';
import { ChatPage } from './chat.page';
import { ToolsPage } from './tools.page';
import { EventsPage } from './events.page';
import { SystemPage } from './system.page';
import { ModelsPage } from './models.page';
import { MemoryPage } from './memory.page';
import { SettingsLayout } from './settings';
import { SchedulesPage } from './schedules.page';
import { RequestsPage } from './requests.page';
import { SubagentsPage } from './subagents.page';
import { UsagePage } from './usage.page';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  { path: 'chat', component: ChatPage },
  { path: 'chat/:id', component: ChatPage },
  { path: 'schedules', component: SchedulesPage },
  { path: 'requests', component: RequestsPage },
  {
    path: 'settings', component: SettingsLayout, children: [
      { path: '', pathMatch: 'full', redirectTo: 'agents' },
      { path: 'agents', component: AgentsPage },
      { path: 'models', component: ModelsPage },
      { path: 'subagents', component: SubagentsPage },
      { path: 'usage', component: UsagePage },
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
