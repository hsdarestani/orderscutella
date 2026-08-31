<?php
/**
 * Plugin Name: Cutella Bot Bridge
 * Description: Secure signed bridge between the Cutella Telegram bot and WooCommerce.
 * Version: 1.0.1
 * Author: Cutella
 * Requires at least: 6.0
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) { exit; }

define('CBB_TOKEN_OPTION', 'cutella_bot_bridge_token');
define('CBB_VERSION', '1.0.1');
require_once __DIR__ . '/chunk-upload.php';
require_once __DIR__ . '/product-type.php';

register_activation_hook(__FILE__, function () {
    if (!get_option(CBB_TOKEN_OPTION)) { update_option(CBB_TOKEN_OPTION, wp_generate_password(48, false, false), false); }
    if (function_exists('cbb_repair_existing_variable_products')) { cbb_repair_existing_variable_products(); }
});
function cbb_get_token() {
    $token = (string)get_option(CBB_TOKEN_OPTION, '');
    if ($token === '') { $token = wp_generate_password(48, false, false); update_option(CBB_TOKEN_OPTION, $token, false); }
    return $token;
}
function cbb_endpoint_url() { return home_url('/?cbb=2'); }
function cbb_no_cache() {
    nocache_headers();
    header('X-Robots-Tag: noindex, nofollow', true);
    header('X-Cutella-Bridge: ' . CBB_VERSION, true);
}
function cbb_fail($message, $status=400) { cbb_no_cache(); wp_send_json_error(array('message'=>(string)$message), (int)$status); }
function cbb_b64url_decode($value) {
    $value = strtr((string)$value, '-_', '+/');
    $pad = strlen($value) % 4; if ($pad) { $value .= str_repeat('=', 4-$pad); }
    return base64_decode($value, true);
}

add_action('admin_menu', function () {
    add_submenu_page('woocommerce','Cutella Bot Bridge','Cutella Bot Bridge','manage_woocommerce','cutella-bot-bridge','cbb_admin_page');
});
function cbb_admin_page() {
    if (!current_user_can('manage_woocommerce')) { wp_die('Access denied.'); }
    if (isset($_POST['cbb_regenerate'])) {
        check_admin_referer('cbb_regenerate_token');
        update_option(CBB_TOKEN_OPTION, wp_generate_password(48, false, false), false);
        echo '<div class="notice notice-success"><p>توکن جدید ساخته شد.</p></div>';
    }
    $token = esc_attr(cbb_get_token());
    ?>
    <div class="wrap"><h1>Cutella Bot Bridge</h1>
    <p>اتصال امن ربات Cutella به WooCommerce با Signed GET + HMAC.</p>
    <p><strong>Version:</strong> <?php echo esc_html(CBB_VERSION); ?> &nbsp;|&nbsp; <strong>cURL Multi:</strong> <?php echo function_exists('curl_multi_init') ? 'Available' : 'Compatibility mode'; ?></p>
    <table class="form-table"><tr><th>Bridge Endpoint</th><td><code><?php echo esc_html(cbb_endpoint_url()); ?></code></td></tr>
    <tr><th>Bridge Token</th><td><input id="cbb-token" type="text" readonly value="<?php echo $token; ?>" style="width:min(720px,100%);font-family:monospace" /> <button type="button" class="button" onclick="navigator.clipboard.writeText(document.getElementById('cbb-token').value);this.innerText='کپی شد ✓';">کپی</button></td></tr></table>
    <form method="post" onsubmit="return confirm('توکن قبلی فوراً باطل شود؟');"><?php wp_nonce_field('cbb_regenerate_token'); ?><button class="button" name="cbb_regenerate" value="1">ساخت توکن جدید</button></form>
    </div><?php
}

function cbb_v2_request() { return isset($_GET['cbb']) && (string)$_GET['cbb'] === '2'; }
function cbb_v2_authorize() {
    $ts = isset($_GET['t']) ? (string)wp_unslash($_GET['t']) : '';
    $nonce = isset($_GET['n']) ? (string)wp_unslash($_GET['n']) : '';
    $op = isset($_GET['o']) ? sanitize_key(wp_unslash($_GET['o'])) : '';
    $data = isset($_GET['d']) ? (string)wp_unslash($_GET['d']) : '';
    $sig = isset($_GET['s']) ? strtolower((string)wp_unslash($_GET['s'])) : '';
    if (!ctype_digit($ts) || abs(time()-intval($ts)) > 300) { cbb_fail('Expired bridge request.', 401); }
    if (!preg_match('/^[a-f0-9]{20,64}$/',$nonce) || !preg_match('/^[a-f0-9]{64}$/',$sig) || $op === '') { cbb_fail('Invalid bridge signature.', 401); }
    $message = 'v2|' . $ts . '|' . $nonce . '|' . $op . '|' . $data;
    $expected = hash_hmac('sha256', $message, cbb_get_token());
    if (!hash_equals($expected, $sig)) { cbb_fail('Invalid bridge signature.', 401); }
    $nonce_key = 'cbb_nonce_' . md5($nonce);
    if (get_transient($nonce_key)) { cbb_fail('Replay rejected.', 409); }
    set_transient($nonce_key, 1, 10*MINUTE_IN_SECONDS);
    $payload = array();
    if ($data !== '') {
        $compressed = cbb_b64url_decode($data);
        $json = $compressed === false ? false : @gzuncompress($compressed);
        $decoded = $json === false ? null : json_decode($json, true);
        if (!is_array($decoded)) { cbb_fail('Invalid payload.', 400); }
        $payload = $decoded;
    }
    return array($op, $payload);
}

function cbb_product_payload($product) {
    return array('id'=>(int)$product->get_id(),'name'=>(string)$product->get_name(),'price'=>(string)$product->get_price(),'stock_quantity'=>$product->get_stock_quantity(),'permalink'=>(string)get_permalink($product->get_id()));
}
function cbb_order_payload($order) {
    return array(
        'id'=>(int)$order->get_id(),
        'status'=>(string)$order->get_status(),
        'billing'=>array('first_name'=>$order->get_billing_first_name(),'last_name'=>$order->get_billing_last_name(),'phone'=>$order->get_billing_phone()),
        'shipping'=>array('first_name'=>$order->get_shipping_first_name(),'last_name'=>$order->get_shipping_last_name()),
        'meta_data'=>array(array('key'=>'_cutella_tracking_code','value'=>(string)$order->get_meta('_cutella_tracking_code', true))),
    );
}
function cbb_image_ids($images) {
    $ids=array(); foreach ((array)$images as $image) { if (is_array($image) && !empty($image['id'])) { $ids[]=absint($image['id']); } }
    return array_values(array_unique(array_filter($ids)));
}
function cbb_create_product($data) {
    $type = isset($data['type']) ? sanitize_key($data['type']) : 'simple';
    $product = $type === 'variable' ? new WC_Product_Variable() : new WC_Product_Simple();
    $product->set_name(sanitize_text_field(isset($data['name'])?$data['name']:''));
    $product->set_status(isset($data['status'])?sanitize_key($data['status']):'publish');
    $category_ids=array(); foreach ((array)($data['categories']??array()) as $cat) { if (is_array($cat)&&!empty($cat['id'])) {$category_ids[]=absint($cat['id']);} }
    if ($category_ids) {$product->set_category_ids(array_values(array_unique($category_ids)));}
    $image_ids=cbb_image_ids($data['images']??array()); if ($image_ids) {$product->set_image_id($image_ids[0]); if(count($image_ids)>1){$product->set_gallery_image_ids(array_slice($image_ids,1));}}
    if ($type==='simple') {
        $product->set_manage_stock(!empty($data['manage_stock']));
        if (!empty($data['manage_stock'])) {$product->set_stock_quantity(max(0,intval($data['stock_quantity']??0)));}
        if (isset($data['regular_price']) && $data['regular_price']!=='') {$product->set_regular_price(wc_format_decimal($data['regular_price']));}
        if (isset($data['sale_price']) && $data['sale_price']!=='') {$product->set_sale_price(wc_format_decimal($data['sale_price']));}
    } else {
        $attrs=array(); foreach ((array)($data['attributes']??array()) as $raw) {
            if (!is_array($raw)||empty($raw['name'])) continue;
            $a=new WC_Product_Attribute(); $a->set_id(0); $a->set_name(sanitize_text_field($raw['name']));
            $opts=array_values(array_filter(array_map('sanitize_text_field',(array)($raw['options']??array()))));
            $a->set_options($opts); $a->set_visible(true); $a->set_variation(true); $attrs[]=$a;
        }
        if ($attrs) {$product->set_attributes($attrs);}
    }
    $id=$product->save(); if(!$id){throw new Exception('Product could not be saved.');}
    if ($type==='variable') {cbb_force_variable_product_type($id);}
    return array('id'=>(int)$id,'name'=>(string)$product->get_name(),'permalink'=>(string)get_permalink($id),'type'=>$type);
}
function cbb_create_variation($data) {
    $product_id=absint($data['product_id']??0); $payload=is_array($data['variation']??null)?$data['variation']:array();
    if(!$product_id||!wc_get_product($product_id)){throw new Exception('Parent product not found.');}
    $v=new WC_Product_Variation(); $v->set_parent_id($product_id);
    if(isset($payload['regular_price'])&&$payload['regular_price']!==''){$v->set_regular_price(wc_format_decimal($payload['regular_price']));}
    if(isset($payload['sale_price'])&&$payload['sale_price']!==''){$v->set_sale_price(wc_format_decimal($payload['sale_price']));}
    $v->set_manage_stock(!empty($payload['manage_stock'])); if(!empty($payload['manage_stock'])){$v->set_stock_quantity(max(0,intval($payload['stock_quantity']??0)));}
    $attrs=array(); foreach((array)($payload['attributes']??array()) as $raw){if(is_array($raw)&&!empty($raw['name'])){$attrs[sanitize_title($raw['name'])]=sanitize_text_field($raw['option']??'');}}
    $v->set_attributes($attrs); $id=$v->save(); if(!$id){throw new Exception('Variation could not be saved.');}
    cbb_force_variable_product_type($product_id); WC_Product_Variable::sync($product_id); wc_delete_product_transients($product_id);
    return array('id'=>(int)$id,'parent_id'=>$product_id,'permalink'=>(string)get_permalink($product_id));
}
function cbb_dispatch($op,$payload) {
    if (!class_exists('WooCommerce')) { cbb_fail('WooCommerce is not active.',503); }
    try {
        switch($op){
            case 'ping':
                $counts=wp_count_posts('product'); $total=0; foreach(array('publish','draft','pending','private') as $s){if(isset($counts->{$s})){$total+=(int)$counts->{$s};}}
                wp_send_json_success(array('version'=>CBB_VERSION,'product_count'=>$total,'site'=>home_url('/'),'curl_multi'=>function_exists('curl_multi_init'))); break;
            case 'categories':
                $terms=get_terms(array('taxonomy'=>'product_cat','hide_empty'=>false)); if(is_wp_error($terms)){throw new Exception($terms->get_error_message());}
                $items=array(); foreach($terms as $t){$items[]=array('id'=>(int)$t->term_id,'name'=>(string)$t->name,'parent'=>(int)$t->parent,'count'=>(int)$t->count);} wp_send_json_success(array('categories'=>$items)); break;
            case 'recent_products':
                $count=max(1,min(20,intval($payload['count']??10))); $products=wc_get_products(array('limit'=>$count,'orderby'=>'date','order'=>'DESC','status'=>array('publish','draft','pending','private')));
                wp_send_json_success(array('products'=>array_map('cbb_product_payload',$products))); break;
            case 'create_product': wp_send_json_success(cbb_create_product($payload)); break;
            case 'create_variation': wp_send_json_success(cbb_create_variation($payload)); break;
            case 'media_begin': wp_send_json_success(cbb_media_begin($payload)); break;
            case 'media_chunk': wp_send_json_success(cbb_media_chunk($payload)); break;
            case 'media_finish': wp_send_json_success(cbb_media_finish($payload)); break;
            case 'orders':
                $statuses=array_map('sanitize_key',(array)($payload['statuses']??array('processing','completed','on-hold'))); $limit=max(1,min(5000,intval($payload['limit']??2000)));
                $orders=wc_get_orders(array('limit'=>$limit,'status'=>$statuses,'orderby'=>'date','order'=>'DESC','return'=>'objects'));
                wp_send_json_success(array('orders'=>array_map('cbb_order_payload',$orders))); break;
            case 'update_order_tracking':
                $order_id=absint($payload['order_id']??0); $code=preg_replace('/\D+/','',(string)($payload['tracking_code']??'')); $order=wc_get_order($order_id);
                if(!$order||$code===''){throw new Exception('Invalid order or tracking code.');}
                $current=trim((string)$order->get_meta('_cutella_tracking_code',true));
                if($current!==''&&$current!==$code){throw new Exception('overwrite protected');}
                $order->update_meta_data('_cutella_tracking_code',$code); $order->save();
                wp_send_json_success(array('id'=>$order_id,'tracking_code'=>$code)); break;
            default: cbb_fail('Unknown operation.',400);
        }
    } catch(Throwable $e){ cbb_fail($e->getMessage(),500); }
}
function cbb_handle_request(){
    if(!cbb_v2_request()) return;
    list($op,$payload)=cbb_v2_authorize(); cbb_no_cache(); cbb_dispatch($op,$payload); exit;
}
add_action('plugins_loaded','cbb_handle_request',PHP_INT_MAX);
add_action('template_redirect','cbb_handle_request',0);
