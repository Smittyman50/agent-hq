import { Routes } from '@angular/router';
import { AgentsPage } from './agents.page';
import { ChatPage } from './chat.page';
import { ToolsPage } from './tools.page';
import { EventsPage } from './events.page';
import { SystemPage } from './system.page';

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'chat' },
  { path: 'chat', component: ChatPage },
  { path: 'chat/:id', component: ChatPage },
  { path: 'agents', component: AgentsPage },
  { path: 'tools', component: ToolsPage },
  { path: 'events', component: EventsPage },
  { path: 'system', component: SystemPage },
];
