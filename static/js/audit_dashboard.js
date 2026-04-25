// Global state
let allData = [];
let filteredData = [];
const INACTIVE_THRESHOLD_DAYS = 30;

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    refreshData();
});

// Show toast notification
function showToast(message, type = 'success') {
    const toast = document.getElementById('toast');
    const toastIcon = document.getElementById('toast-icon');
    const toastMessage = document.getElementById('toast-message');
    
    toastIcon.textContent = type === 'success' ? '✅' : type === 'error' ? '❌' : 'ℹ️';
    toastMessage.textContent = message;
    
    toast.classList.remove('translate-y-20', 'opacity-0');
    
    setTimeout(() => {
        toast.classList.add('translate-y-20', 'opacity-0');
    }, 3000);
}

// Fetch data from backend
async function refreshData() {
    try {
        showToast('Carregando dados...', 'info');
        
        const data = await get_authors_activity();
        allData = data.map(row => ({
            ...row,
            days_inactive: calculateDaysInactive(row.last_activity)
        }));
        
        updateStats();
        filterTable();
        
        showToast('Dados atualizados com sucesso!');
    } catch (error) {
        console.error('Error fetching data:', error);
        showToast('Erro ao carregar dados: ' + error.message, 'error');
        
        document.getElementById('authors-table-body').innerHTML = `
            <tr>
                <td colspan="7" class="px-6 py-8 text-center text-rose-500">
                    ❌ Erro ao carregar dados. Verifique o console para mais detalhes.
                </td>
            </tr>
        `;
    }
}

// Calculate days since last activity
function calculateDaysInactive(lastActivity) {
    const lastDate = new Date(lastActivity);
    const now = new Date();
    const diffTime = Math.abs(now - lastDate);
    return Math.ceil(diffTime / (1000 * 60 * 60 * 24));
}

// Update statistics cards
function updateStats() {
    const total = allData.length;
    const active = allData.filter(row => row.days_inactive <= INACTIVE_THRESHOLD_DAYS).length;
    const inactive = total - active;
    
    document.getElementById('stat-total').textContent = total;
    document.getElementById('stat-active').textContent = active;
    document.getElementById('stat-inactive').textContent = inactive;
    document.getElementById('stat-updated').textContent = new Date().toLocaleTimeString('pt-BR');
}

// Filter table based on search and status
function filterTable() {
    const searchTerm = document.getElementById('search-input').value.toLowerCase();
    const statusFilter = document.getElementById('status-filter').value;
    
    filteredData = allData.filter(row => {
        // Search filter
        const matchesSearch = !searchTerm || 
            (row.author_id && row.author_id.toLowerCase().includes(searchTerm)) ||
            (row.author_email && row.author_email.toLowerCase().includes(searchTerm));
        
        // Status filter
        const isActive = row.days_inactive <= INACTIVE_THRESHOLD_DAYS;
        const matchesStatus = statusFilter === 'all' ||
            (statusFilter === 'active' && isActive) ||
            (statusFilter === 'inactive' && !isActive);
        
        return matchesSearch && matchesStatus;
    });
    
    sortTable();
}

// Sort table based on selected option
function sortTable() {
    const sortBy = document.getElementById('sort-by').value;
    
    filteredData.sort((a, b) => {
        switch (sortBy) {
            case 'last_activity_desc':
                return new Date(b.last_activity) - new Date(a.last_activity);
            case 'last_activity_asc':
                return new Date(a.last_activity) - new Date(b.last_activity);
            case 'events_desc':
                return (b.total_events || 0) - (a.total_events || 0);
            case 'events_asc':
                return (a.total_events || 0) - (b.total_events || 0);
            default:
                return 0;
        }
    });
    
    renderTable();
}

// Get CSS class for days inactive
function getDaysClass(days) {
    if (days <= 7) return 'days-recent';
    if (days <= 30) return 'days-warning';
    return 'days-danger';
}

// Render table rows
function renderTable() {
    const tbody = document.getElementById('authors-table-body');
    
    if (filteredData.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="7" class="px-6 py-8 text-center text-slate-500">
                    Nenhum resultado encontrado.
                </td>
            </tr>
        `;
        return;
    }
    
    tbody.innerHTML = filteredData.map(row => {
        const isActive = row.days_inactive <= INACTIVE_THRESHOLD_DAYS;
        const statusClass = isActive ? 'status-active' : 'status-inactive';
        const statusText = isActive ? 'Ativo' : 'Inativo';
        const daysClass = getDaysClass(row.days_inactive);
        
        return `
            <tr class="hover:bg-slate-50">
                <td class="px-6 py-4">
                    <code class="text-xs bg-slate-100 px-2 py-1 rounded truncate-cell" title="${escapeHtml(row.author_id || 'N/A')}">
                        ${escapeHtml(truncateString(row.author_id || 'N/A', 20))}
                    </code>
                </td>
                <td class="px-6 py-4 text-sm text-slate-700 truncate-cell" title="${escapeHtml(row.author_email || 'N/A')}">
                    ${escapeHtml(row.author_email || 'N/A')}
                </td>
                <td class="px-6 py-4 text-sm text-slate-700">
                    ${formatDateTime(row.last_activity)}
                </td>
                <td class="px-6 py-4 text-sm ${daysClass}">
                    ${row.days_inactive} dias
                </td>
                <td class="px-6 py-4 text-sm text-slate-700">
                    ${row.total_events || 0}
                </td>
                <td class="px-6 py-4 text-sm text-slate-500 truncate-cell" title="${escapeHtml(row.last_event_name || 'N/A')}">
                    ${escapeHtml(truncateString(row.last_event_name || 'N/A', 25))}
                </td>
                <td class="px-6 py-4">
                    <span class="status-badge ${statusClass}">
                        ${statusText}
                    </span>
                </td>
            </tr>
        `;
    }).join('');
}

// Format date for display
function formatDateTime(dateString) {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    return date.toLocaleString('pt-BR', {
        day: '2-digit',
        month '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

// Truncate string with ellipsis
function truncateString(str, maxLength) {
    if (!str || str.length <= maxLength) return str;
    return str.substring(0, maxLength) + '...';
}

// Escape HTML to prevent XSS
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Export data to CSV
function exportCSV() {
    if (filteredData.length === 0) {
        showToast('Nenhum dado para exportar', 'error');
        return;
    }
    
    const headers = ['Author ID', 'Email', 'Última Atividade', 'Dias Inativo', 'Total Eventos', 'Último Evento', 'Status'];
    const rows = filteredData.map(row => [
        row.author_id || '',
        row.author_email || '',
        row.last_activity || '',
        row.days_inactive,
        row.total_events || 0,
        row.last_event_name || '',
        row.days_inactive <= INACTIVE_THRESHOLD_DAYS ? 'Ativo' : 'Inativo'
    ]);
    
    const csvContent = [
        headers.join(','),
        ...rows.map(row => row.map(cell => `"${String(cell).replace(/"/g, '""')}"`).join(','))
    ].join('\n');
    
    const blob = new Blob(['\ufeff' + csvContent], { type: 'text/csv;charset=utf-8;' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `authors_activity_${new Date().toISOString().split('T')[0]}.csv`;
    link.click();
    
    showToast('CSV exportado com sucesso!');
}
