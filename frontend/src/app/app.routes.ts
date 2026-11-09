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
import { LoginPage } from './login.page';
import { SecurityPage } from './security.page';
import { SkillsPage } from './skills.page';
import { authGuard } from './session';

export const routes: Routes = [
  { path: 'login', component: LoginPage },
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  { path: 'chat', component: ChatPage, canActivate: [authGuard] },
  { path: 'chat/:id', component: ChatPage, canActivate: [authGuard] },
  { path: 'schedules', component: SchedulesPage, canActivate: [authGuard] },
  { path: 'requests', component: RequestsPage, canActivate: [authGuard] },
  {
    path: 'settings', component: SettingsLayout, canActivate: [authGuard], children: [
      { path: '', pathMatch: 'full', redirectTo: 'agents' },
      { path: 'agents', component: AgentsPage },
      { path: 'models', component: ModelsPage },
      { path: 'subagents', component: SubagentsPage },
      { path: 'usage', component: UsagePage },
      { path: 'memory', component: MemoryPage },
      { path: 'skills', component: SkillsPage },
      { path: 'tools', component: ToolsPage },
      { path: 'events', component: EventsPage },
      { path: 'system', component: SystemPage },
      { path: 'security', component: SecurityPage },
    ],
  },
  // Old addresses (bookmarks) from before Settings existed.
  ...['agents', 'models', 'memory', 'tools', 'events', 'system'].map(p => ({ path: p, redirectTo: `settings/${p}` })),
  { path: '**', redirectTo: 'chat' },
];
