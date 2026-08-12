<?php declare(strict_types = 0);

namespace Modules\WebTerm\Actions;

use API;
use CController;

class HostInfo extends CController {
    protected function init(): void {
        // AJAX fetch from the module JS does not send a CSRF token.
        $this->disableCsrfValidation();
    }

    protected function checkInput(): bool {
        return $this->validateInput([
            'hostid' => 'required|id'
        ]);
    }

    protected function checkPermissions(): bool {
        return $this->getUserType() >= USER_TYPE_ZABBIX_ADMIN;
    }

    protected function doAction(): void {
        $hostid = $this->getInput('hostid');

        $hosts = API::Host()->get([
            'output' => ['hostid', 'name', 'host'],
            'selectInterfaces' => ['interfaceid', 'ip', 'dns', 'port', 'type', 'main'],
            'hostids' => [$hostid]
        ]);

        $host = $hosts ? $hosts[0] : null;
        $ip = '';
        if ($host && !empty($host['interfaces'])) {
            foreach ($host['interfaces'] as $iface) {
                if ($iface['main'] == 1) {
                    $ip = !empty($iface['ip']) ? $iface['ip'] : $iface['dns'];
                    break;
                }
            }
            if (!$ip) {
                $ip = !empty($host['interfaces'][0]['ip'])
                    ? $host['interfaces'][0]['ip']
                    : $host['interfaces'][0]['dns'];
            }
        }

        header('Content-Type: application/json');
        echo json_encode([
            'host' => $host,
            'ip' => $ip
        ]);
        exit;
    }
}
