<?php

namespace Kanboard\Plugin\MarcusDevEnv\Model;

use Kanboard\Model\TaskModel;

/**
 * TaskModel that tells Marcus when a task is deleted.
 *
 * Kanboard has no deletion event to listen for: TaskModel::remove() drops
 * the row without dispatching anything, and there is no EVENT_REMOVE
 * constant at all (checked against v1.2.53) — so no amount of
 * `$this->on(...)` in a plugin can observe a deletion, and the outbound
 * webhook Kanboard ships never carries one either.
 *
 * Without this, Marcus only learns a ticket is gone by noticing it missing
 * on a later board read. That works, but it is not instant, and a ticket
 * deleted while Marcus is stopped is invisible until its next startup
 * reconcile. Overriding the model is the one place a deletion can actually
 * be observed.
 *
 * Deliberately fire-and-forget: deleting a task in Kanboard must never
 * fail, hang, or show an error because Marcus happens to be down. The
 * request uses a short timeout and every failure is swallowed.
 */
class NotifyingTaskModel extends TaskModel
{
    /**
     * Remove a task, then tell Marcus it is gone.
     *
     * @param  integer $task_id
     * @return boolean
     */
    public function remove($task_id)
    {
        // Read the task BEFORE deleting it — afterwards there is nothing
        // left to look up, and the payload needs its project id.
        $task = $this->taskFinderModel->getById($task_id);

        $removed = parent::remove($task_id);

        if ($removed) {
            $this->notifyMarcus($task_id, is_array($task) ? $task : array());
        }

        return $removed;
    }

    /**
     * POST a task.remove event to Marcus's Kanboard webhook endpoint.
     *
     * Mirrors the shape Kanboard's own outbound webhook uses, so Marcus's
     * receiver parses it with the same code path as every other event.
     *
     * @param  integer $task_id
     * @param  array   $task
     * @return void
     */
    private function notifyMarcus($task_id, array $task)
    {
        $marcusUrl = getenv('MARCUS_URL') ?: 'http://localhost:4298';
        $token = getenv('KANBOARD_WEBHOOK_TOKEN') ?: '';

        $url = rtrim($marcusUrl, '/') . '/webhooks/kanboard';
        if ($token !== '') {
            $url .= '?token=' . rawurlencode($token);
        }

        $payload = json_encode(array(
            'event_name' => 'task.remove',
            'event_data' => array(
                'task_id' => $task_id,
                'task' => array(
                    'id' => $task_id,
                    'project_id' => isset($task['project_id'])
                        ? $task['project_id']
                        : null,
                ),
            ),
        ));

        // Short timeouts: this runs inline in the request that deletes the
        // task, so a slow or absent Marcus must not stall the Kanboard UI.
        $context = stream_context_create(array(
            'http' => array(
                'method' => 'POST',
                'header' => "Content-Type: application/json\r\n",
                'content' => $payload,
                'timeout' => 2,
                // Do not raise on 4xx/5xx — there is nothing useful to do
                // with a failure here, and the board read still catches the
                // deletion regardless.
                'ignore_errors' => true,
            ),
        ));

        // @ suppresses connection warnings leaking into the Kanboard page:
        // Marcus being down is an expected, non-fatal condition.
        @file_get_contents($url, false, $context);
    }
}
