<?php

namespace Kanboard\Plugin\MarcusDevEnv;

use Kanboard\Core\Plugin\Base;

/**
 * MarcusDevEnv — Kanboard plugin
 *
 * Adds a "View Live Changes" sidebar button to every task.
 * Clicking the button calls the Marcus HTTP endpoint that spins up
 * (or looks up an already-running) dev environment for that ticket
 * and redirects the browser to the hot-reload URL.
 *
 * Configuration
 * -------------
 * Set the environment variable MARCUS_URL to the base URL of your Marcus
 * MCP server (e.g. http://localhost:4298). Falls back to that default.
 */
class Plugin extends Base
{
    /**
     * Called by Kanboard when the plugin is loaded.
     */
    public function initialize(): void
    {
        $this->template->hook->attach(
            'template:task:sidebar:information',
            'MarcusDevEnv:task/sidebar'
        );
        $this->template->hook->attach(
            'template:board:private:header',
            'MarcusDevEnv:board/header'
        );
    }

    /**
     * Plugin metadata shown in the Kanboard plugin manager.
     */
    public function getPluginName(): string
    {
        return 'Marcus Dev Environment';
    }

    public function getPluginDescription(): string
    {
        return 'Adds a "View Live Changes" button to each task that spins up a hot-reload dev environment via Marcus.';
    }

    public function getPluginAuthor(): string
    {
        return 'Marcus';
    }

    public function getPluginVersion(): string
    {
        return '1.0.0';
    }

    public function getPluginHomepage(): string
    {
        return 'https://github.com/aak540114/marcus';
    }
}
