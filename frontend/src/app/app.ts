import { Component } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';

@Component({
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  selector: 'app-root',
  template: `
    <nav class="top">
      <span class="brand">Agent HQ</span>
      <a routerLink="/chat" routerLinkActive="on">Chat</a>
      <a routerLink="/agents" routerLinkActive="on">Agents</a>
      <a routerLink="/tools" routerLinkActive="on">Tools</a>
      <a routerLink="/events" routerLinkActive="on">Events</a>
      <a routerLink="/system" routerLinkActive="on">System</a>
    </nav>
    <main><router-outlet /></main>
  `,
})
export class App {}
