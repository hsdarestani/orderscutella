<?php
/**
 * Plugin Name: OrdersCutella Tracking
 * Description: Displays postal tracking codes written by the OrdersCutella Telegram bot.
 * Version: 1.0.0
 * Author: Cutella
 */

if (!defined('ABSPATH')) {
    exit;
}

const ORDERSCUTELLA_TRACKING_META = '_cutella_tracking_code';

function orderscutella_tracking_value($order) {
    if (!$order instanceof WC_Order) {
        $order = wc_get_order($order);
    }
    if (!$order) {
        return '';
    }
    return trim((string) $order->get_meta(ORDERSCUTELLA_TRACKING_META, true));
}

add_action('woocommerce_admin_order_data_after_shipping_address', function ($order) {
    $code = orderscutella_tracking_value($order);
    if (!$code) {
        return;
    }
    echo '<p><strong>' . esc_html__('کد رهگیری پستی:', 'orderscutella') . '</strong> ' . esc_html($code) . '</p>';
});

add_action('woocommerce_order_details_after_order_table', function ($order) {
    $code = orderscutella_tracking_value($order);
    if (!$code) {
        return;
    }
    echo '<section class="woocommerce-order-tracking-code">';
    echo '<h2>' . esc_html__('رهگیری مرسوله', 'orderscutella') . '</h2>';
    echo '<p><strong>' . esc_html__('کد رهگیری پستی:', 'orderscutella') . '</strong> ' . esc_html($code) . '</p>';
    echo '</section>';
});

add_filter('woocommerce_email_order_meta_fields', function ($fields, $sent_to_admin, $order) {
    $code = orderscutella_tracking_value($order);
    if ($code) {
        $fields['orderscutella_tracking_code'] = array(
            'label' => __('کد رهگیری پستی', 'orderscutella'),
            'value' => $code,
        );
    }
    return $fields;
}, 10, 3);

add_action('add_meta_boxes', function () {
    $screens = array('shop_order');
    if (function_exists('wc_get_page_screen_id')) {
        $screens[] = wc_get_page_screen_id('shop-order');
    }
    foreach (array_unique($screens) as $screen) {
        add_meta_box(
            'orderscutella-tracking',
            __('کد رهگیری Cutella', 'orderscutella'),
            'orderscutella_tracking_metabox',
            $screen,
            'side',
            'default'
        );
    }
});

function orderscutella_tracking_metabox($post_or_order) {
    $order = $post_or_order instanceof WC_Order ? $post_or_order : wc_get_order($post_or_order->ID ?? 0);
    $code = orderscutella_tracking_value($order);
    wp_nonce_field('orderscutella_tracking_save', 'orderscutella_tracking_nonce');
    echo '<p><input style="width:100%" type="text" name="orderscutella_tracking_code" value="' . esc_attr($code) . '" placeholder="کد رهگیری"></p>';
    echo '<p style="color:#666;margin-bottom:0">این فیلد توسط ربات نیز قابل بروزرسانی است.</p>';
}

function orderscutella_tracking_save_order($order_id) {
    if (!isset($_POST['orderscutella_tracking_nonce']) || !wp_verify_nonce(sanitize_text_field(wp_unslash($_POST['orderscutella_tracking_nonce'])), 'orderscutella_tracking_save')) {
        return;
    }
    if (!current_user_can('edit_shop_order', $order_id)) {
        return;
    }
    $order = wc_get_order($order_id);
    if (!$order) {
        return;
    }
    $value = isset($_POST['orderscutella_tracking_code']) ? sanitize_text_field(wp_unslash($_POST['orderscutella_tracking_code'])) : '';
    $order->update_meta_data(ORDERSCUTELLA_TRACKING_META, $value);
    $order->save();
}
add_action('woocommerce_process_shop_order_meta', 'orderscutella_tracking_save_order');

add_action('woocommerce_update_order', function ($order_id) {
    if (!isset($_POST['orderscutella_tracking_nonce'])) {
        return;
    }
    orderscutella_tracking_save_order($order_id);
}, 20);
