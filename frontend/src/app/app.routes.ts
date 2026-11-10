import { Routes } from '@angular/router';
import { AgentsPage } from './agents.page';
import { ChatPage } from './chat.page';
import { ToolsPage } from './tools.page';
import { EventsPage } from './events.page';
import { SystemPage } from './system.page';
import { ModelsPage } from './models.page';
import { MemoryPage } from './memory.page';
import { SettingsLayout } from './settings';
import { LearningPage } from './learning.page';
import { ProfilePage } from './profile.page';
import { SchedulesPage } from './schedules.page';
import { ApprovalsInbox, SkillsInbox, SubagentInbox, ProfileInbox } from './requests.page';
import { RequestsHome, RequestsLayout } from './requests';
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
  {
    path: 'requests', component: RequestsLayout, canActivate: [authGuard], children: [
      { path: '', pathMatch: 'full', component: RequestsHome },
      { path: 'approvals', component: ApprovalsInbox },
      { path: 'skills', component: SkillsInbox },
      { path: 'profile', component: ProfileInbox },
        { path: 'subagents', component: SubagentInbox },
    ],
  },
  {
    path: 'settings', component: SettingsLayout, canActivate: [authGuard], children: [
      { path: '', pathMatch: 'full', redirectTo: 'agents' },
      { path: 'agents', component: AgentsPage },
      { path: 'models', component: ModelsPage },
      { path: 'subagents', component: SubagentsPage },
      { path: 'usage', component: UsagePage },
      { path: 'memory', component: MemoryPage },
      { path: 'skills', component: SkillsPage },
      { path: 'learning', component: LearningPage },
      { path: 'profile', component: ProfilePage },
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
