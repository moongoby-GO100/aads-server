<?php if (!defined('BASEPATH')) exit('No direct script access allowed');

class Goods extends CI_Controller {

    /**
     * Variable for loading the config array into
     * @var array
     */
    private $view_data;
    private $config_vars;
    private $subdomain_name;

	function __construct()
	{
		// ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기
		parent::__construct();

        //debug($this->session->all_userdata());
        //exit;
		//if(!$this->session->userdata('user_id')) redirect('/auth/login/');

        $this->view_data = $this->session->all_userdata();
        $this->view_data['is_mobile'] = 'N';

        $this->load->library('user_agent');
        if ($this->agent->is_mobile()) $this->view_data['is_mobile'] = 'Y';

        $this->config->load('autoscrap');
        $this->config_vars = $this->config->item('market');
        $this->subdomain_name = $this->config->item('subdomain_name');
        //debug($this->subdomain_name);

        $this->load->library('pagination');

        // 공급사 회원 정보 확인
        $this->load->model('tank_auth/users');
        $this->view_data['shop'] = subdomain_user_info($this->subdomain_name);
        // debug($this->view_data);
        if(!$this->view_data['shop']['user_id'])
        {
    		//$this->view_data['shop']['user_id'] = $this->session->userdata('username');
        }
        //debug($this->view_data);

        // 미니몰 로그인 사용 확인(2021.10.21)
        if($this->view_data['shop']['mall_login_used'] == 'Y')
        {
		    if(!$this->session->userdata('user_id')) redirect('/auth/login/');
        }
        // debug($this->view_data);

		// 다운로드 권한 확인
		$this->view_data['down_check'] = user_download_checked($this->view_data);

		$this->view_data['Category1'] = '';
		$this->view_data['Category2'] = '';
		$this->view_data['Category3'] = '';

		$Category1 = $this->_clean_category_value($this->input->get('Category1'), 1);
		$Category2 = $this->_clean_category_value($this->input->get('Category2'), 2);
		$Category3 = $this->_clean_category_value($this->input->get('Category3'), 3);

		if($Category1)
		{
			// $this->session->set_userdata('Category1', $this->input->get('Category1'));
			$this->view_data['Category1'] = $Category1;
		}
		if($Category2)
		{
			// $this->session->set_userdata('Category2', $this->input->get('Category2'));
			$this->view_data['Category2'] = $Category2;
		}
		if($Category3)
		{
			// $this->session->set_userdata('Category3', $this->input->get('Category3'));
			$this->view_data['Category3'] = $Category3;
		}

		$this->view_data['CateData1'] = get_user_category('', '');
		$this->view_data['CateData2'] = get_user_category($this->view_data['Category1'], $this->view_data['shop']['user_id']);
		if($this->view_data['Category2'])
		{
			$cate_depth = '';
			$Category2_arr = explode('|', $this->view_data['Category2']);
			if($Category2_arr[1]) $cate_depth = '|'.($Category2_arr[1]);
			$this->view_data['CateData3'] = get_user_category($Category2_arr[0].$cate_depth, $this->view_data['shop']['user_id']);
		}

        // cbn 서비스를 위한 이미지 도메인 경로 추가(2022.10.26)
        $this->view_data['CDNIMGURL'] = $this->config->item('new_image_domain');
		// debug($this->view_data);

	}

	private function _clean_category_value($value, $depth)
	{
		$value = trim((string)$value);
		if($value === '') return '';

		if($depth == 1 && preg_match('/^[0-9]+$/', $value)) return $value;
		if($depth == 2 && preg_match('/^[0-9]+\|1$/', $value)) return $value;
		if($depth == 3 && preg_match('/^[0-9]+\|2$/', $value)) return $value;

		return '';
	}

	private function _goodscode_image_files($goods_code)
	{
		$img_dir = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$files = glob($img_dir . '*');
		$names = array();
		if ($files) {
			foreach ($files as $f) {
				if (is_file($f)) $names[] = basename($f);
			}
		}
		natsort($names);
		return array_values($names);
	}

			private function _clean_search_suffix($data)
		{
			$suffix = [];

		if(isset($data['Category1']))
			$suffix['Category1'] = $this->_clean_category_value($data['Category1'], 1);
		if(isset($data['Category2']))
			$suffix['Category2'] = $this->_clean_category_value($data['Category2'], 2);
		if(isset($data['Category3']))
			$suffix['Category3'] = $this->_clean_category_value($data['Category3'], 3);
		if(isset($data['search_text']))
			$suffix['search_text'] = trim((string)$data['search_text']);

			return $suffix;
		}

		private function _download_safe_file_part($name, $fallback)
		{
			$name = trim((string)$name);
			if($name === '') $name = $fallback;
			$name = str_replace(array("|","\\","/","?","*","<","\"",":",">"), "_", $name);
			$name = preg_replace('/\s+/', '_', $name);
			$name = trim($name, "._- \t\n\r\0\x0B");
			if($name === '') $name = $fallback;
			if(function_exists('mb_substr')) $name = mb_substr($name, 0, 80, 'UTF-8');
			else $name = substr($name, 0, 80);
			return $name;
		}

		private function _download_unique_zip_filename($prefix, $display_name='')
		{
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$token = substr(md5(uniqid('', TRUE).mt_rand()), 0, 8);
			$name = $this->_download_safe_file_part($display_name, $prefix);
			return $prefix.'_'.$user_id.'_'.date('YmdHis').'_'.$token.'_'.$name.'.zip';
		}

		function index()
		{
		$this->load->model('main_m');

        /**
		 * 최근(1주) 상품이미지 시작
		 */
        if($this->uri->segment(2))
            $page = $this->uri->segment(2);
        else
            $page = 1;

        $num = 12;
        $_goods = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], 4);
		$_goods1 = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], $num, $page);
		//debug($_goods1);
        $this->view_data['GoodsData'] = $_goods->data;
		$this->view_data['GoodsData1'] = $_goods1->data;

        $config['base_url'] = '/goods/';
        $config['total_rows'] = $_goods1->recordsTotal;
        $config['uri_segment'] = 2;

        if($this->uri->segment(2))
        {
            $config['cur_tag_open'] = '<li class="active"><a href="'.$config['base_url'].$this->uri->segment(2).'">';
        }
        else {
            $config['cur_tag_open'] = '<li class="active"><a href="'.$config['base_url'].'">';
        }

        $config['per_page'] = $num;
        $config['num_links'] = 3;
        $config['use_page_numbers'] = TRUE;
        //$config['page_query_string'] = TRUE;
        $config['first_link'] = '&laquo;';
        $config['first_tag_open'] = '<li>';
        $config['first_tag_close'] = '</li>';
        $config['next_link'] = false;
        $config['next_tag_open'] = '<li>';
        $config['next_tag_close'] = '</li>';
        $config['prev_link'] = false;
        $config['prev_tag_open'] = '<li>';
        $config['prev_tag_close'] = '</li>';
        $config['last_link'] = '&raquo;';
        $config['last_tag_open'] = '<li>';
        $config['last_tag_close'] = '</li>';
        $config['cur_tag_close'] = '</a></li>';
        $config['num_tag_open'] = '<li>';
        $config['num_tag_close'] = '</li>';
        //debug($config);
        $this->pagination->initialize($config);
        $this->view_data['GoodsPages'] = $this->pagination;
        //debug($this->pagination);
        //echo $this->pagination->create_links();

		$this->load->view('top', $this->view_data);
		$this->load->view('goods/goods', $this->view_data);
		$this->load->view('bottom', $this->view_data);
	}

    function detail()
	{
        $this->load->helper('html');

        if(!$this->uri->segment(3))
            alert('정상적인 접근이 아닙니다!');

        $goods_id = $this->uri->segment(3);

		$this->load->model('goods_m');

		$this->view_data['goods_array'] = $this->config->item('goods_array');
        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id']){
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
		}else{
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;
			$goods_info->GoodsEtc9 = $goods_info->GoodsEtc33; // shop 도메인 접근시 강제 적용
		}

        $this->view_data['GoodsInfo'] = $goods_info;
        $this->view_data['GoodsSortImg1'] = explode('||', $goods_info->GoodsSortImg1);
        $this->view_data['GoodsSortImg2'] = explode('||', $goods_info->GoodsSortImg2);
        $this->view_data['GoodsSortImg3'] = explode('||', $goods_info->GoodsSortImg3);
        //if ($goods_info->GoodsEtc73 && $goods_info->created >= '2023-12-01') {
        if ($goods_info->GoodsEtc73 && isset($goods_info->ocean_id)) {
        	$this->view_data['CDNIMGURL'] = '';
        	$this->view_data['goods_img_url'] = $goods_info->GoodsEtc73;
        	$this->view_data['GoodsInfoImg1'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_01.jpg';
			$this->view_data['GoodsInfoImg2'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_03.jpg';
			$this->view_data['GoodsInfoImg3'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_04.jpg';
			$this->view_data['GoodsInfoImg4'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_05.jpg';
			$this->view_data['GoodsInfoImg5'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_06.jpg';
			$this->view_data['GoodsInfoImg6'] = $this->view_data['goods_img_url'].$goods_info->GoodsCode.'_07.jpg';
        }
        else {
        	$this->view_data['goods_img_url'] = $this->config->item('user_goodscode_img_url').$goods_info->GoodsCode.'/';
			$this->view_data['GoodsInfoImg1'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
			$this->view_data['GoodsInfoImg2'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
			$this->view_data['GoodsInfoImg3'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
			$this->view_data['GoodsInfoImg4'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
			$this->view_data['GoodsInfoImg5'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
			$this->view_data['GoodsInfoImg6'] = $this->view_data['CDNIMGURL'].'/data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';
		}

		$this->view_data['GoodsInfo']->GoodsEtcUnserializes = [];
		if($this->view_data['GoodsInfo']->GoodsEtcSerializes) {
			$this->view_data['GoodsInfo']->OptionSizeArr = explode(',', $this->view_data['GoodsInfo']->OptionSize);
			$this->view_data['GoodsInfo']->GoodsEtcUnserializes = unserialize($this->view_data['GoodsInfo']->GoodsEtcSerializes);
			// debug($this->view_data['Goods']->OptionSizeArr);
			// debug($this->view_data['GoodsInfo']->GoodsEtcUnserializes);
		}
        // debug($this->view_data['GoodsInfo']);
        // debug($this->view_data);

        $this->view_data['link'] = '
            <link rel="image_src" href="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

//        $this->view_data['meta'] = '
//            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
//            <meta property="og:type" content="website" />
//            <meta property="og:image" content="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
//            <meta property="og:image" content="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
//            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
//            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
//            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
//            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
//            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
//            <meta name="nate:image" content="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
//		';
//if($_SERVER["REMOTE_ADDR"] == '121.134.129.200'){
//	echo "<xmp>";
//	print_r($goods_info);
//	echo "</xmp>";
//
//}
		// 메타 타이틀 없을 경우에는 일반 쇼핑몰명으로 대체
		$meta_title = ($this->view_data['shop']['shop_title_meta'] != '') ? $this->view_data['shop']['shop_title_meta'] : $this->view_data['shop']['shop_name'];

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$meta_title.' - '.$goods_info->GoodsEtc5.'" />            
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.$this->view_data['CDNIMGURL'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$meta_title.'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
			<meta property="og:description" content="'.preg_replace('/\r\n|\r|\n/','',$this->view_data['shop']['shop_description']).'" />
			<meta property="title" content="'.$meta_title.' - '.$goods_info->GoodsEtc5.'" />
			<meta property="description" content="'. preg_replace('/\r\n|\r|\n/','',$this->view_data['shop']['shop_description']).'" />
			<meta name="description" content="'. preg_replace('/\r\n|\r|\n/','',$this->view_data['shop']['shop_description']).'" />
			<meta name="naver-site-verification" content="'.$this->view_data['shop']['shop_naver_meta'].'" />
			<meta name="google-site-verification" content="'.$this->view_data['shop']['shop_google_meta'].'" />
		';
		/*
        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta property="og:description" content="'.$goods_info->GoodsEtc36.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:description" content="'.$goods_info->GoodsEtc36.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';
		*/
        //debug($this->view_data['meta']);
        // $meta = array(
        //             array('property' => 'og:title', 'content' => $goods_info->GoodsName),
        //             array('property' => 'og:type', 'content' => 'website'),
        //             array('property' => 'og:image', 'content' => BASEURL.'goods/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0),
        //             array('property' => 'og:image', 'content' => BASEURL.'goods/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1),
        //             array('property' => 'og:site_name', 'content' => $this->view_data['shop']['shop_name']),
        //             array('property' => 'og:url', 'content' => BASEURL),
        //             array('property' => 'og:description', 'content' => $goods_info->GoodsEtc36)
        //     );
        // debug(meta($meta));

		// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") {
        //     debug($this->view_data['GoodsInfo']);exit;
        // }
		$this->view_data['re'] = isset($_REQUEST['re'])?$_REQUEST['re']:'';

		$this->load->view('top', $this->view_data);
		$this->view_data['goods_no'] = $goods_id;

		// 찜 상태 체크
		$wishCheck = $this->goods_m->wishCheck($this->session->userdata('user_id'), $goods_id);						
		$this->view_data['wishCheck'] = $wishCheck;

		// if( $this->view_data['GoodsInfo']->GoodsSortImg1 && $this->view_data['GoodsInfo']->GoodsSortImg2 && $this->view_data['GoodsInfo']->GoodsSortImg3 )
		// 상품별 상세 스킨을 저장했을 때로 반영(2020.12.31)
		if( $this->view_data['GoodsInfo']->DeSkin != 'N' )
		{
			// if($_SERVER["REMOTE_ADDR"] == "218.157.131.10")
				// $this->load->view('goods/detail_new_frame', $this->view_data);
			// else
                // if($_SERVER["REMOTE_ADDR"] == "218.157.131.10") {}
                // else
                    $this->view_data['DetailCheck'] = 1;
//				if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){						
//				    $this->load->view('goods/detail_test2', $this->view_data);
//				}else{
						$this->load->view('goods/detail_new_frame', $this->view_data);
//				}
		}
		else{
//		 if($_SERVER["REMOTE_ADDR"] == "121.134.129.200"){
//			$this->load->view('goods/detail_test2', $this->view_data);
//		 }else{
				 $this->load->view('goods/detail', $this->view_data);
//		 }
		}

		$this->load->view('bottom', $this->view_data);
    }

    function detail_save()
	{
        // debug('detail_save');
		log_message("info", "controllers detail_save function");
        $this->load->helper('html');

		$goods_id = $_GET['goodsId'];
		$check = (isset($_GET['check']))?$_GET['check']:'';
		$de_skin = (isset($_GET['de_skin']))?$_GET['de_skin']:'';	// 상세 스킨
		$mo_skin = (isset($_GET['mo_skin']))?$_GET['mo_skin']:'';	// MO 스킨
        if(!$goods_id)
            alert('정상적인 접근이 아닙니다!');
		// debug('goods_id');

		$this->load->model('goods_m');

		$this->view_data['goods_array'] = $this->config->item('goods_array');
        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id'])
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
        else
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;

		$this->view_data['DetailCheck'] = $check;
        $this->view_data['GoodsInfo'] = $goods_info;
        $this->view_data['GoodsSortImg1'] = explode('||', $goods_info->GoodsSortImg1);
        $this->view_data['GoodsSortImg2'] = explode('||', $goods_info->GoodsSortImg2);
        $this->view_data['GoodsSortImg3'] = explode('||', $goods_info->GoodsSortImg3);
        $this->view_data['GoodsSortImg4'] = explode('||', $goods_info->GoodsSortImg4);
		$this->view_data['goods_img_url'] = $this->config->item('user_goodscode_img_url').$goods_info->GoodsCode.'/';
        // debug($this->view_data);
		$this->view_data['GoodsInfoImg1'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
		$this->view_data['GoodsInfoImg2'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
		$this->view_data['GoodsInfoImg3'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
		$this->view_data['GoodsInfoImg4'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
		$this->view_data['GoodsInfoImg5'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
		$this->view_data['GoodsInfoImg6'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';

		$this->view_data['GoodsInfo']->GoodsEtcUnserializes = [];
		if($this->view_data['GoodsInfo']->GoodsEtcSerializes) {
			$this->view_data['GoodsInfo']->OptionSizeArr = explode(',', $this->view_data['GoodsInfo']->OptionSize);
			$this->view_data['GoodsInfo']->GoodsEtcUnserializes = unserialize($this->view_data['GoodsInfo']->GoodsEtcSerializes);
			// debug($this->view_data['Goods']->OptionSizeArr);
			// debug($this->view_data['Goods']->GoodsEtcUnserializes);
		}
        // debug($this->view_data['GoodsInfo']);
        // debug($this->view_data['GoodsSortImg2']);

        $this->view_data['link'] = '
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';

		$this->view_data['de_skin'] = $de_skin;
		$this->view_data['mo_skin'] = $mo_skin;
		if($de_skin) $this->view_data['skinfile'] = 'goods/detail_skin_frame';
		if($mo_skin) $this->view_data['skinfile'] = 'goods/detail_mo'.$mo_skin;

		// $this->load->view('top', $this->view_data);
		$this->load->view('goods/detail_img_save', $this->view_data);
		// $this->load->view('bottom', $this->view_data);
	}

    function detail_save_test1()
	{
        // debug('detail_save');
        $this->load->helper('html');

		$goods_id = $_GET['goodsId'];
		$check = (isset($_GET['check']))?$_GET['check']:'';
        if(!$goods_id)
            alert('정상적인 접근이 아닙니다!');
		// debug('goods_id');

		$this->load->model('goods_m');

		$this->view_data['goods_array'] = $this->config->item('goods_array');
        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id'])
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
        else
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;

		$this->view_data['DetailCheck'] = $check;
        $this->view_data['GoodsInfo'] = $goods_info;
        $this->view_data['GoodsSortImg1'] = explode('||', $goods_info->GoodsSortImg1);
        $this->view_data['GoodsSortImg2'] = explode('||', $goods_info->GoodsSortImg2);
        $this->view_data['GoodsSortImg3'] = explode('||', $goods_info->GoodsSortImg3);
        $this->view_data['GoodsSortImg4'] = explode('||', $goods_info->GoodsSortImg4);
		$this->view_data['goods_img_url'] = $this->config->item('user_goodscode_img_url').$goods_info->GoodsCode.'/';
        // debug($this->view_data);
		$this->view_data['GoodsInfoImg1'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
		$this->view_data['GoodsInfoImg2'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
		$this->view_data['GoodsInfoImg3'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
		$this->view_data['GoodsInfoImg4'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
		$this->view_data['GoodsInfoImg5'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
		$this->view_data['GoodsInfoImg6'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';

		$this->view_data['GoodsInfo']->GoodsEtcUnserializes = [];
		if($this->view_data['GoodsInfo']->GoodsEtcSerializes) {
			$this->view_data['GoodsInfo']->OptionSizeArr = explode(',', $this->view_data['GoodsInfo']->OptionSize);
			$this->view_data['GoodsInfo']->GoodsEtcUnserializes = unserialize($this->view_data['GoodsInfo']->GoodsEtcSerializes);
			// debug($this->view_data['Goods']->OptionSizeArr);
			// debug($this->view_data['Goods']->GoodsEtcUnserializes);
		}
        // debug($this->view_data['GoodsInfo']);
        // debug($this->view_data['GoodsSortImg1']);

        $this->view_data['link'] = '
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';

		// $this->load->view('top', $this->view_data);
		$this->load->view('goods/detail_img_save_test1', $this->view_data);
		$this->load->view('bottom', $this->view_data);
	}

    function detail_save_test2()
	{
        // debug('detail_save');
        $this->load->helper('html');

		$goods_id = $_GET['goodsId'];
		$check = (isset($_GET['check']))?$_GET['check']:'';
        if(!$goods_id)
            alert('정상적인 접근이 아닙니다!');
		// debug('goods_id');

		$this->load->model('goods_m');

		$this->view_data['goods_array'] = $this->config->item('goods_array');
        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id'])
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
        else
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;

		$this->view_data['DetailCheck'] = $check;
        $this->view_data['GoodsInfo'] = $goods_info;
        $this->view_data['GoodsSortImg1'] = explode('||', $goods_info->GoodsSortImg1);
        $this->view_data['GoodsSortImg2'] = explode('||', $goods_info->GoodsSortImg2);
        $this->view_data['GoodsSortImg3'] = explode('||', $goods_info->GoodsSortImg3);
        $this->view_data['GoodsSortImg4'] = explode('||', $goods_info->GoodsSortImg4);
		$this->view_data['goods_img_url'] = $this->config->item('user_goodscode_img_url').$goods_info->GoodsCode.'/';
        // debug($this->view_data);
		$this->view_data['GoodsInfoImg1'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
		$this->view_data['GoodsInfoImg2'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
		$this->view_data['GoodsInfoImg3'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
		$this->view_data['GoodsInfoImg4'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
		$this->view_data['GoodsInfoImg5'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
		$this->view_data['GoodsInfoImg6'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';

		$this->view_data['GoodsInfo']->GoodsEtcUnserializes = [];
		if($this->view_data['GoodsInfo']->GoodsEtcSerializes) {
			$this->view_data['GoodsInfo']->OptionSizeArr = explode(',', $this->view_data['GoodsInfo']->OptionSize);
			$this->view_data['GoodsInfo']->GoodsEtcUnserializes = unserialize($this->view_data['GoodsInfo']->GoodsEtcSerializes);
			// debug($this->view_data['Goods']->OptionSizeArr);
			// debug($this->view_data['Goods']->GoodsEtcUnserializes);
		}
        // debug($this->view_data['GoodsInfo']);
        // debug($this->view_data['GoodsSortImg1']);

        $this->view_data['link'] = '
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';

		// $this->load->view('top', $this->view_data);
		$this->load->view('goods/detail_img_save_test2', $this->view_data);
		$this->load->view('bottom', $this->view_data);
	}

    function images()
	{
		$this->load->library('image_lib');
		$GoodsCode = $this->input->post('GoodsCode');
		$img_name = $this->input->post('img-name');

		if(!$GoodsCode || !$img_name) {
			echo json_encode(array('success'=>false,'msg'=>'필수값 누락'));
			exit;
		}

		$base_dir = '/home/danharoo/www/data/files/goods/goodscode/img/'.$GoodsCode.'/';
		$file = $base_dir.$img_name.'.jpg';
		$thumb_dir = $base_dir.'thumbnail/';

		// 디렉토리 없으면 생성
		if(!is_dir($base_dir)) mkdir($base_dir, 0755, true);
		if(!is_dir($thumb_dir)) mkdir($thumb_dir, 0755, true);

		// 기존 파일이 있으면 삭제
		if(file_exists($file)) {
			@unlink($file);
			@unlink($thumb_dir.$img_name.'.jpg');
		}

		$saved = false;

		// 방법1: Blob 파일 업로드 (toBlob 방식 - base64 변환 불필요, 전송량 40% 감소)
		if(isset($_FILES['img-file']) && $_FILES['img-file']['error'] === UPLOAD_ERR_OK) {
			$saved = move_uploaded_file($_FILES['img-file']['tmp_name'], $file);
		}
		// 방법2: base64 방식 (기존 호환)
		else {
			$img_data = $this->input->post('img-capture');
			if($img_data) {
				$uri = substr($img_data, strpos($img_data, ",") + 1);
				if($uri) {
					$saved = (file_put_contents($file, base64_decode($uri)) !== false);
				}
			}
		}

		if($saved)
		{
			$config['source_image']	= $file;
			$config['new_image']	= $thumb_dir;
			$config['image_library'] = 'gd';
			$config['maintain_ratio'] = TRUE;
			$config['width']	= 100;
			$config['height']	= 100;
			$this->image_lib->initialize($config);
			$this->image_lib->resize();
		}

		echo '/data/files/goods/goodscode/img/'.$GoodsCode.'/'.$img_name.'.jpg';
		exit;
	}


	/**
	 * 하이브리드 이미지 합성 - 서버사이드 ImageMagick
	 * 순수 이미지 섹션(_01, _03~_13)을 서버에서 직접 세로 합성
	 * html2canvas 불필요 -> 87초 -> ~15초 단축
	 */
	function images_compose()
	{
		header('Content-Type: application/json');

		$GoodsId = $this->input->post('GoodsId');
		$GoodsCode = $this->input->post('GoodsCode');
		$section = $this->input->post('section');

		if(!$GoodsId || !$GoodsCode || !$section) {
			echo json_encode(array('success'=>false, 'msg'=>'required params missing'));
			exit;
		}

		$detail = $this->db->select('GoodsSortImg1, GoodsSortImg2, GoodsSortImg3')
			->where('goods_id', $GoodsId)
			->get('goods_detail')
			->row();

		if(!$detail) {
			echo json_encode(array('success'=>false, 'msg'=>'goods_detail not found'));
			exit;
		}

		$img1 = array_filter(explode('||', $detail->GoodsSortImg1));
		$img2 = array_filter(explode('||', $detail->GoodsSortImg2));
		$img3 = array_filter(explode('||', $detail->GoodsSortImg3));

		$source_files = array();
		$sec = intval($section);

		if($sec == 1) {
			$source_files = array_values($img1);
		} elseif($sec >= 3 && $sec <= 12) {
			$offset = ($sec - 3) * 6;
			$source_files = array_values(array_slice($img2, $offset, 6));
		} elseif($sec == 13) {
			$source_files = array_values($img3);
		} else {
			echo json_encode(array('success'=>false, 'msg'=>'unsupported section'));
			exit;
		}

		$source_files = array_filter($source_files, function($f) { return trim($f) !== ''; });

		if(empty($source_files)) {
			echo json_encode(array('success'=>false, 'msg'=>'no images for section '.$section));
			exit;
		}

		$base_dir = '/home/danharoo/www/data/files/goods/goodscode/img/'.strtolower($GoodsCode).'/';
		$valid_files = array();
		foreach($source_files as $fname) {
			$full = $base_dir . $fname;
			if(file_exists($full)) {
				$valid_files[] = $full;
			}
		}

		if(empty($valid_files)) {
			echo json_encode(array('success'=>false, 'msg'=>'source files not found'));
			exit;
		}

		$out_dir = $base_dir;
		if(!is_dir($out_dir)) mkdir($out_dir, 0755, true);
		$num = ($sec < 10) ? '0'.$sec : strval($sec);
		$out_file = $out_dir . strtolower($GoodsCode) . '_' . $num . '.jpg';

		$escaped = array();
		foreach($valid_files as $f) {
			$escaped[] = escapeshellarg($f);
		}
		$cmd = 'convert ' . implode(' ', $escaped) . ' -resize 900x -append -quality 85 ' . escapeshellarg($out_file) . ' 2>&1';

		$output = array();
		$ret = 0;
		exec($cmd, $output, $ret);

		if($ret !== 0) {
			echo json_encode(array('success'=>false, 'msg'=>'ImageMagick failed', 'detail'=>implode(' ', $output)));
			exit;
		}

		$thumb_dir = $out_dir . 'thumbnail/';
		if(!is_dir($thumb_dir)) mkdir($thumb_dir, 0755, true);
		$thumb_file = $thumb_dir . strtolower($GoodsCode) . '_' . $num . '.jpg';
		exec('convert ' . escapeshellarg($out_file) . ' -resize 100x100 -quality 80 ' . escapeshellarg($thumb_file) . ' 2>&1');

		$filesize = filesize($out_file);
		echo json_encode(array(
			'success' => true,
			'section' => $section,
			'path' => '/data/files/goods/goodscode/img/'.strtolower($GoodsCode).'/'.strtolower($GoodsCode).'_'.$num.'.jpg',
			'size' => round($filesize / 1024),
			'images' => count($valid_files)
		));
		exit;
	}

	/**
	 * 하이브리드 이미지 합성 - 전체 섹션 ONE SHOT
	 * _01, _03~_13 모든 섹션을 단일 AJAX 요청으로 처리 (ImageMagick)
	 * 12회 개별 호출 → 1회 일괄 처리로 속도 대폭 개선
	 */
	function server_compose()
	{
		header('Content-Type: application/json');
		@set_time_limit(120);

		$GoodsId   = $this->input->post('GoodsId');
		$GoodsCode = $this->input->post('GoodsCode');

		if (!$GoodsId || !$GoodsCode) {
			echo json_encode(array('success' => false, 'msg' => 'required params missing'));
			exit;
		}

		$detail = $this->db->select('GoodsSortImg1, GoodsSortImg2, GoodsSortImg3')
			->where('goods_id', $GoodsId)
			->get('goods_detail')
			->row();

		if (!$detail) {
			echo json_encode(array('success' => false, 'msg' => 'goods_detail not found'));
			exit;
		}

		$img1 = array_values(array_filter(explode('||', $detail->GoodsSortImg1)));
		$img2 = array_values(array_filter(explode('||', $detail->GoodsSortImg2)));
		$img3 = array_values(array_filter(explode('||', $detail->GoodsSortImg3)));

		$base_dir  = '/home/danharoo/www/data/files/goods/goodscode/img/' . strtolower($GoodsCode) . '/';
		$thumb_dir = $base_dir . 'thumbnail/';

		if (!is_dir($base_dir))  mkdir($base_dir,  0755, true);
		if (!is_dir($thumb_dir)) mkdir($thumb_dir, 0755, true);

		// 섹션별 소스 이미지 매핑
		// _01: GoodsSortImg1 전체
		// _03~_12: GoodsSortImg2 (offset=(sec-3)*6, 6장씩)
		// _13: GoodsSortImg3 전체
		$section_map = array();
		$section_map[1] = $img1;
		for ($i = 3; $i <= 12; $i++) {
			$offset = ($i - 3) * 6;
			$section_map[$i] = array_values(array_slice($img2, $offset, 6));
		}
		$section_map[13] = $img3;

		$results = array();

		foreach ($section_map as $sec => $source_files) {
			$source_files = array_values(array_filter($source_files, function($f) { return trim($f) !== ''; }));

			if (empty($source_files)) {
				$results[$sec] = array('success' => false, 'msg' => 'no images for section ' . $sec);
				continue;
			}

			$valid_files = array();
			foreach ($source_files as $fname) {
				$full = $base_dir . $fname;
				if (file_exists($full)) {
					$valid_files[] = $full;
				}
			}

			if (empty($valid_files)) {
				$results[$sec] = array('success' => false, 'msg' => 'source files not found for section ' . $sec);
				continue;
			}

			$num      = ($sec < 10) ? '0' . $sec : strval($sec);
			$out_file = $base_dir . strtolower($GoodsCode) . '_' . $num . '.jpg';

			$escaped = array();
			foreach ($valid_files as $f) {
				$escaped[] = escapeshellarg($f);
			}
			$cmd = 'convert ' . implode(' ', $escaped) . ' -resize 900x -append -quality 85 ' . escapeshellarg($out_file) . ' 2>&1';

			$output_lines = array();
			$ret = 0;
			exec($cmd, $output_lines, $ret);

			if ($ret !== 0) {
				$results[$sec] = array('success' => false, 'msg' => 'compose failed', 'detail' => implode(' ', $output_lines));
				continue;
			}

			// 썸네일 생성
			$thumb_file = $thumb_dir . strtolower($GoodsCode) . '_' . $num . '.jpg';
			exec('convert ' . escapeshellarg($out_file) . ' -resize 100x100 -quality 80 ' . escapeshellarg($thumb_file) . ' 2>&1');

			$filesize = filesize($out_file);
			$results[$sec] = array(
				'success' => true,
				'section' => $num,
				'path'    => '/data/files/goods/goodscode/img/' . strtolower($GoodsCode) . '/' . strtolower($GoodsCode) . '_' . $num . '.jpg',
				'size'    => round($filesize / 1024),
				'images'  => count($valid_files)
			);
		}

		$success_count = count(array_filter($results, function($r) { return $r['success']; }));

		echo json_encode(array(
			'success'  => ($success_count > 0),
			'total'    => count($results),
			'done'     => $success_count,
			'sections' => $results
		));
		exit;
	}

	//
    function images_save_end()
	{
		// debug($_REQUEST);exit;
		$GoodsId = $this->input->post('GoodsId');
		$GoodsCode = $this->input->post('GoodsCode');
		$de_skin = $this->input->post('de_skin');
		$mo_skin = $this->input->post('mo_skin');

		if(!$GoodsId) {
			echo '실패!';
			exit;
		}

		$this->load->model('goods_m');
        $goods_info = $this->goods_m->goods_info($GoodsId, $this->view_data['shop']['user_id']);
		// debug($goods_info);

		$goods_data1 = array(
			'GoodsDetailSave'		=> 'Y',
			'GoodsDetailSaveDay'	=> date('Y-m-d', time())
		);

		if($de_skin) $goods_data1['DeSkin'] = $de_skin;
		if($mo_skin) $goods_data1['MoSkin'] = $mo_skin;

		$this->db->where('id', $GoodsId);
		$this->db->update('goods', $goods_data1);


		// 상세정렬2 저장은 단하루상품설명 반영 안함(2021.01.21)
		if($mo_skin)
		{
			echo '성공!';
			exit;
		}

		/************** 단하루상품설명 자동반영(2020.12.04 추가) html 내용 수정(2020.12.04)  ***************/
		$goods_info_html_data = $this->config->item('goods_info_html_data_new'.$de_skin); // de_skin 별 처리(2021.01.05)
        // 이미지경로 외부도메인 반영(2018.02.20)
        if($goods_info->GoodsEtc73)
            $goods_info_html_data = str_replace("{GoodsImgPath}", $goods_info->GoodsEtc73, $goods_info_html_data);
        else
            $goods_info_html_data = str_replace("{GoodsImgPath}", "https://img.newtalk.kr/data/files/goods/img/".$goods_info->GoodsCode."/", $goods_info_html_data);

		// 이미지 생성파일 체크 후 있으면 반영
		for($i=3; $i < 21; $i++)
		{
			$num = $i;
			if($i < 10) $num = '0'.$i;

            // 이미지 생성파일 체크 후 있으면 반영 재반영(2021.06.15)
			$url_file = strtolower($goods_info->GoodsCode).'/'.strtolower($goods_info->GoodsCode).'_'.$num.'.jpg';
			// $url_file = '';
			$file = '/home/danharoo/www/data/files/goods/goodscode/img/'.$url_file;
            // debug($file);
			if(file_exists($file))
			{
				if($goods_info->GoodsEtc73) {
					$url_file = strtolower($goods_info->GoodsCode).'_'.$num.'.jpg';
					$replace_img = '<br /><IMG src="'.$goods_info->GoodsEtc73.$url_file.'">';
				}
				else {
					// $url_file = strtolower($goods_info->GoodsCode).'/'.strtolower($goods_info->GoodsCode).'_'.$num.'.jpg';
					$replace_img = '<br /><IMG src="https://img.newtalk.kr/data/files/goods/img/'.$url_file.'">';
				}
                // debug($replace_img);

				$goods_info_html_data = str_replace("{GoodsCode_".$num."}", $replace_img, $goods_info_html_data);
			}
			else {
                // debug($file);
				$goods_info_html_data = str_replace("{GoodsCode_".$num."}", "", $goods_info_html_data);
			}
		}

		$goods_info_html_data = str_replace("{GoodsCode}", strtolower($goods_info->GoodsCode), $goods_info_html_data);

		// 방어: DanharooGoodsName에 상품코드가 포함된 경우 제거
		$danharoo_name = $goods_info->DanharooGoodsName;
		$code_upper = strtoupper($goods_info->GoodsCode);
		$code_lower = strtolower($goods_info->GoodsCode);
		if(stripos($danharoo_name, $code_upper.'-') === 0) {
			$danharoo_name = substr($danharoo_name, strlen($code_upper) + 1);
		} elseif(stripos($danharoo_name, $code_lower.'-') === 0) {
			$danharoo_name = substr($danharoo_name, strlen($code_lower) + 1);
		}
		$goods_info_html_data = str_replace("{GoodsName}", $danharoo_name, $goods_info_html_data);
		$goods_info_html_data = str_replace("{Description}", nl2br($goods_info->Description), $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc24}", '['.$goods_info->GoodsEtc24.']', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc25}", $goods_info->GoodsEtc25, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc26}", $goods_info->GoodsEtc26, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc16}", $goods_info->GoodsEtc16, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionColor}", $goods_info->OptionColor, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc15}", $goods_info->GoodsEtc15, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc13}", ',사이즈('.$goods_info->GoodsEtc13.')', $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc18}", $goods_info->GoodsEtc18, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc17}", $goods_info->GoodsEtc17, $goods_info_html_data);
		$goods_info_html_data = str_replace("{OptionSize}", $goods_info->OptionSize, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc14}", nl2br($goods_info->GoodsEtc14), $goods_info_html_data);
		$goods_info_html_data = str_replace("{MakerName}", $goods_info->MakerName, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc20}", $goods_info->GoodsEtc20, $goods_info_html_data);
		$goods_info_html_data = str_replace("{GoodsEtc21}", $goods_info->GoodsEtc21, $goods_info_html_data);

		$DanharooDescription = $goods_info_html_data;	// 단하루상품설명
		/************** 단하루상품설명 자동반영(2020.12.04 추가)  ***************/

		// 상품별 부가이미지11, 부가이미지12 파일명 변경
		$GoodsEtc69 = str_replace("-270.gif", "-g_1.gif", $goods_info->GoodsEtc69);
		$GoodsEtc70 = str_replace("-270.gif", "-g_2.gif", $goods_info->GoodsEtc70);

		$goods_data2 = array(
			'DanharooDescription'	=> $DanharooDescription,
			'GoodsEtc69'			=> $GoodsEtc69,
			'GoodsEtc70'			=> $GoodsEtc70
		);
		$this->db->where('goods_id', $GoodsId);
		$this->db->update('goods_detail', $goods_data2);

		echo '성공!';
		exit;
	}

    function detail_new1()
	{
        $this->load->helper('html');

        $goods_id = '30536';	// t5636k0b

		$this->load->model('goods_m');

		$this->view_data['goods_array'] = $this->config->item('goods_array');
        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id'])
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
        else
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;

        $this->view_data['GoodsInfo'] = $goods_info;
        $this->view_data['GoodsSortImg1'] = explode('||', $goods_info->GoodsSortImg1);
        $this->view_data['GoodsSortImg2'] = explode('||', $goods_info->GoodsSortImg2);
        $this->view_data['GoodsSortImg3'] = explode('||', $goods_info->GoodsSortImg3);
		$this->view_data['goods_img_url'] = $this->config->item('user_goodscode_img_url').$goods_info->GoodsCode.'/';
        //debug($this->view_data);
		$this->view_data['GoodsInfoImg1'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
		$this->view_data['GoodsInfoImg2'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
		$this->view_data['GoodsInfoImg3'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
		$this->view_data['GoodsInfoImg4'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
		$this->view_data['GoodsInfoImg5'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
		$this->view_data['GoodsInfoImg6'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';

		$this->view_data['GoodsInfo']->GoodsEtcUnserializes = [];
		if($this->view_data['GoodsInfo']->GoodsEtcSerializes) {
			$this->view_data['GoodsInfo']->OptionSizeArr = explode(',', $this->view_data['GoodsInfo']->OptionSize);
			$this->view_data['GoodsInfo']->GoodsEtcUnserializes = unserialize($this->view_data['GoodsInfo']->GoodsEtcSerializes);
			// debug($this->view_data['Goods']->OptionSizeArr);
			// debug($this->view_data['Goods']->GoodsEtcUnserializes);
		}
        // debug($this->view_data['GoodsInfo']);
		// debug($this->view_data['GoodsSortImg1']);

        $this->view_data['link'] = '
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';

		$this->load->view('top', $this->view_data);
		$this->load->view('goods/detail_test', $this->view_data);
		$this->load->view('bottom', $this->view_data);
	}

    function detail_new2()
	{
        $this->load->helper('html');

        $goods_id = '29021';

		$this->load->model('goods_m');

        $goods_info = $this->goods_m->goods_info($goods_id, $this->view_data['shop']['user_id']);
        // debug($goods_info);
        if($this->view_data['shop']['id'])
            $goods_info->GoodsName = $goods_info->GoodsEtc5;
        else
            $goods_info->GoodsEtc41 = $goods_info->GoodsEtc33;

        $this->view_data['GoodsInfo'] = $goods_info;
        //debug($this->view_data);
		$this->view_data['GoodsInfoImg1'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_01.jpg';
		$this->view_data['GoodsInfoImg2'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_03.jpg';
		$this->view_data['GoodsInfoImg3'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_04.jpg';
		$this->view_data['GoodsInfoImg4'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_05.jpg';
		$this->view_data['GoodsInfoImg5'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_06.jpg';
		$this->view_data['GoodsInfoImg6'] = BASEURL.'data/files/goods/goodscode/img/'.$goods_info->GoodsCode.'/'.$goods_info->GoodsCode.'_07.jpg';

        $this->view_data['link'] = '
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <link rel="image_src" href="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
        ';

        $this->view_data['meta'] = '
            <meta property="og:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta property="og:type" content="website" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
            <meta property="og:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1.'" />
            <meta property="og:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta property="og:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:title" content="'.$goods_info->GoodsEtc5.'" />
            <meta name="nate:site_name" content="'.$this->view_data['shop']['shop_name'].'" />
            <meta name="nate:url" content="'.BASEURL.'goods/detail/'.$goods_info->id.'" />
            <meta name="nate:image" content="'.BASEURL.'data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img0.'" />
		';

		$this->load->view('top', $this->view_data);
		$this->load->view('goods/detail_new2', $this->view_data);
		$this->load->view('bottom', $this->view_data);
	}

    function search()
	{
		$this->load->model('main_m');
		$suffix = $this->_clean_search_suffix($this->input->get());

        //if(!$SearchText) $SearchText = '전체';

		//debug($this->uri->segment(3));

        if($this->uri->segment(3))
            $page = $this->uri->segment(3);
        else
            $page = 1;

        $num = 12;
        //$_goods = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], 4);
		$_goods1 = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], $num, $page, $suffix);
		//debug($_goods1);
        //$this->view_data['GoodsData'] = $_goods->data;
        $this->view_data['GoodsDataTotal'] = $_goods1->recordsTotal;
        $this->view_data['SearchText'] = isset($suffix['search_text']) ? $suffix['search_text'] : '';
		$this->view_data['GoodsData1'] = $_goods1->data;

        $config['base_url'] = '/goods/search/';
		$config['suffix'] = '?'.http_build_query($suffix, '', "&amp;");
        $config['total_rows'] = $_goods1->recordsTotal;
        $config['uri_segment'] = 3;

        $config['cur_tag_open'] = '<li class="active"><a href="javascript://">';
		$config['first_url'] = $config['base_url'] . $config['suffix'];

        $config['per_page'] = $num;
        $config['num_links'] = 3;
        $config['use_page_numbers'] = TRUE;
		$config['reuse_query_string'] = TRUE;
        //$config['page_query_string'] = TRUE;
        $config['first_link'] = '&laquo;';
        $config['first_tag_open'] = '<li>';
        $config['first_tag_close'] = '</li>';
        $config['next_link'] = false;
        $config['next_tag_open'] = '<li>';
        $config['next_tag_close'] = '</li>';
        $config['prev_link'] = false;
        $config['prev_tag_open'] = '<li>';
        $config['prev_tag_close'] = '</li>';
        $config['last_link'] = '&raquo;';
        $config['last_tag_open'] = '<li>';
        $config['last_tag_close'] = '</li>';
        $config['cur_tag_close'] = '</a></li>';
        $config['num_tag_open'] = '<li>';
        $config['num_tag_close'] = '</li>';
        //debug($config);
        $this->pagination->initialize($config);
        $this->view_data['GoodsPages'] = $this->pagination;
        //debug($this->pagination);
        //echo $this->pagination->create_links();

		$this->load->view('top', $this->view_data);

//		if($_SERVER["REMOTE_ADDR"] == '121.134.129.200'){	
//			echo "<xmp>";
//			print_r($this->view_data);
//			echo "</xmp>";
//			$this->load->view('goods/search_test', $this->view_data);
//		}else{
			$this->load->view('goods/search', $this->view_data);
//		}
		$this->load->view('bottom', $this->view_data);
	}

	// 검색 결과 페이징 Add 2023.11.28
	function search_paging(){
		$this->load->model('main_m');

        $start = (int)$this->input->post('start');
		$list = (int)$this->input->post('list');
		if($list < 1) $list = 12;
		$page = ((int)($start / $list)) + 1;
		$suffix = $this->_clean_search_suffix($this->input->post());

        //$_goods = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], 4);
		$_goods1 = $this->main_m->goods_ajax_list('', $this->view_data['shop']['user_id'], $list, $page, $suffix);
		$this->view_data['GoodsData1'] = $_goods1->data;

		$this->load->view('/goods/search_paging', $this->view_data);
	}

	function logs()
	{
		$this->load->library('fire_log');

		/*
		$user_id = $this->session->userdata('user_id');
		//debug($this->session->all_userdata());

		$to_day = date('Y-m-d', time());
		$log_file = '/home/autoda/logs/log-'.$to_day.'.php';

		//$this->load->spark($log_file);

		$this->view_data['to_day'] = $to_day;

		if(is_file($log_file))
			$this->view_data['log_contents'] = file_get_contents($log_file, NULL, NULL);
		else
			$this->view_data['log_contents'] = '';

		$this->load->view('top');
		$this->load->view('logs', $this->view_data);
		$this->load->view('bottom');
		*/
	}

	// 단하루 카테고리 정보 -> 공급회원 상품별 카테고리로 수정(2021.03.31)
	function get_category($code='', $url=true)
	{
		$user_id = $this->view_data['shop']['user_id']; // 공급회원별 구분
		$rtn = true;
		$data = [];

		if($url && @$this->uri->segment(3))
		{
			$rtn = false;
			$code = $this->uri->segment(3);
			if(@$this->uri->segment(4)) $code .= '|'.$this->uri->segment(4);
		}
		$code = trim((string)$code);
		if($code !== '' && !preg_match('/^[0-9]+(\|[12])?$/', $code))
		{
			if($rtn) return [];
			echo '[]';
			return;
		}
		$user_id_sql = $this->db->escape($user_id);
		$code_sql = $this->db->escape($code);
		//debug('code : '.$code);

		if($code)
		{
			$code_arr = explode('|', $code);
			//debug($code_arr);

			if(count($code_arr) == 1)
			{
				//debug($code);
				$sql = "
						SELECT
							GS.Category2,
							REPLACE(GS.Category2, '|1', '') AS MiddleCode,
							GSC.MiddleCategory
						FROM
							goods		AS GS LEFT OUTER JOIN
							goods_cate	As GSC	ON REPLACE(GS.Category2, '|1', '') = GSC.id
						WHERE";
				if($user_id)
				{
					$sql .= "
							GS.GoodsEtc6=".$user_id_sql."
							AND ";
				}
				$sql .= "
							( GS.GoodsEtc52='2' OR GS.GoodsEtc52='3' )
							AND
							GS.mall_activated='Y'
							AND
							GS.Category2!=''
							AND
							GS.Category1=".$code_sql."
						GROUP BY
							GSC.MiddleCategory
						ORDER BY
							GSC.MiddleCategory asc
				";
				// debug($sql);exit;

				$query = $this->db->query($sql);
				$i = 0;
				foreach ($query->result() as $row)
				{
					if($row->MiddleCategory)
					{
						$data[$i]['Code'] = $row->MiddleCode;
						$data[$i]['Name'] = $row->MiddleCategory;
						$i++;
					}
				}

				if(count($data) > 0)
					$data = json_encode($data);
				else
					$data = '[]';
			}
			else
			{
				// debug($code);
				if($code_arr[1] == '1')
				{
					// debug($code);
					$sql = "
							SELECT
								GS.Category3,
								REPLACE(GS.Category3, '|2', '') AS SmallCode,
								GSC.SmallCategory
							FROM
								goods		AS GS LEFT OUTER JOIN
								goods_cate	As GSC	ON REPLACE(GS.Category3, '|2', '') = GSC.id
							WHERE";
					if($user_id)
					{
						$sql .= "
								GS.GoodsEtc6=".$user_id_sql."
								AND ";
					}
					$sql .= "
								( GS.GoodsEtc52='2' OR GS.GoodsEtc52='3' )
								AND
								GS.mall_activated='Y'
								AND
								GS.Category3!=''
								AND
								GS.Category2=".$code_sql."
							GROUP BY
								GSC.SmallCategory
							ORDER BY
								GSC.SmallCategory asc
					";
					$query = $this->db->query($sql);
					$i = 0;
					foreach ($query->result() as $row)
					{
						if($row->SmallCategory)
						{
							$data[$i]['Code'] = $row->SmallCode;
							$data[$i]['Name'] = $row->SmallCategory;
							$i++;
						}
					}

					if(count($data) > 0)
						$data = json_encode($data);
					else
						$data = '[]';
				}
				elseif($code_arr[1] == '2')
				{
					// debug($code);
					$sql = "
							SELECT
								GS.Category3,
								REPLACE(GS.Category3, '|2', '') AS SmallCode,
								GSC.SmallCategory
							FROM
								goods		AS GS LEFT OUTER JOIN
								goods_cate	As GSC	ON REPLACE(GS.Category3, '|2', '') = GSC.id
							WHERE";
					if($user_id)
					{
						$sql .= "
								GS.GoodsEtc6=".$user_id_sql."
								AND ";
					}
					$sql .= "
								( GS.GoodsEtc52='2' OR GS.GoodsEtc52='3' )
								AND
								GS.mall_activated='Y'
								AND
								GS.Category3!=''
								AND
								GS.Category2=".$code_sql."
							GROUP BY
								GSC.SmallCategory
							ORDER BY
								GSC.SmallCategory asc
					";
					// debug($sql);
					$query = $this->db->query($sql);
					$i = 0;
					foreach ($query->result() as $row)
					{
						if($row->SmallCategory)
						{
							$data[$i]['Code'] = $row->SmallCode;
							$data[$i]['Name'] = $row->SmallCategory;
							$i++;
						}
					}

					if(count($data) > 0)
						$data = json_encode($data);
					else
						$data = '[]';
				}
				else $data = '[]';
			}
		}
		else
		{
			$sql = "
					SELECT
						GS.Category1,
						GSC.LargeCategory
					FROM
						goods		AS GS LEFT OUTER JOIN
						goods_cate	As GSC	ON GS.Category1 = GSC.id
					WHERE";
			if($user_id)
			{
				$sql .= "
						GS.GoodsEtc6=".$user_id_sql."
						AND ";
			}
			$sql .= "
						( GS.GoodsEtc52='2' OR GS.GoodsEtc52='3' )
						AND
						GS.mall_activated='Y'
						AND
						GS.Category1!=''
					GROUP BY
						GSC.LargeCategory
					ORDER BY
						GSC.LargeCategory asc
			";
			$query = $this->db->query($sql);
			if($query->num_rows() > 0)
			{
				$i = 0;
				foreach ($query->result() as $row)
				{
					$data[$i]['Code'] = $row->Category1;
					$data[$i]['Name'] = $row->LargeCategory;
					$i++;
				}
			}
		}

		//debug($data);

		if($rtn)
			return $data;
		else
			echo $data;
	}

	// 단하루 카테고리 정보
	function get_category_old($code='', $url=true)
	{
		$user_id = $this->view_data['shop']['user_id']; // 공급회원별 구분
		$rtn = true;

		if($url && @$this->uri->segment(3))
		{
			$rtn = false;
			$code = $this->uri->segment(3);
			if(@$this->uri->segment(4)) $code .= '|'.$this->uri->segment(4);
		}
		//debug('code : '.$code);

		if($code)
		{
			$code_arr = explode('|', $code);
			//debug($code_arr);

			if(count($code_arr) == 1)
			{
				//debug($code);
				$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
				if($query->num_rows() > 0)
				{
					$row = $query->row();
					//debug($row);

					$query = $this->db->query("SELECT id, MiddleCategory FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and activated='Y' group by MiddleCategory order by MiddleCode asc");
					$i = 0;
					foreach ($query->result() as $row)
					{
						if($row->MiddleCategory)
						{
							$data[$i]['Code'] = $row->id;
							$data[$i]['Name'] = $row->MiddleCategory;
							$i++;
						}
					}

					if(count($data) > 0)
						$data = json_encode($data);
					else
						$data = '[]';
				}
			}
			else
			{
				if($code_arr[1] == '1')
				{
					//debug($code);
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code_arr[0]}");
					if($query->num_rows() > 0)
					{
						$row = $query->row();
						//debug($row);

						$query = $this->db->query("SELECT id, MiddleCategory FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and activated='Y' group by MiddleCategory order by MiddleCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->MiddleCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->MiddleCategory;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				elseif($code_arr[1] == '2')
				{
					$code = $code_arr[0];
					$query = $this->db->query("SELECT * FROM goods_cate WHERE market='H' and id={$code}");
					if($query->num_rows() > 0)
					{
						$row = $query->row();

						$query = $this->db->query("SELECT id, SmallCategory, CategoryCode FROM goods_cate WHERE market='H' and LargeCategory='{$row->LargeCategory}' and MiddleCategory='{$row->MiddleCategory}' and activated='Y' group by SmallCategory order by SmallCode asc");
						$i = 0;
						foreach ($query->result() as $row)
						{
							if($row->SmallCategory)
							{
								$data[$i]['Code'] = $row->id;
								$data[$i]['Name'] = $row->SmallCategory;
								$i++;
							}
						}

						if(count($data) > 0)
							$data = json_encode($data);
						else
							$data = '[]';
					}
				}
				else $data = '[]';
			}

		}
		else
		{
			$query = $this->db->query("SELECT id, LargeCategory FROM goods_cate WHERE market='H' and activated='Y' group by LargeCategory order by LargeCode asc");
			if($query->num_rows() > 0)
			{
				$i = 0;
				foreach ($query->result() as $row)
				{
					$data[$i]['Code'] = $row->id;
					$data[$i]['Name'] = $row->LargeCategory;
					$i++;
				}
			}
		}

		//debug($data);

		if($rtn)
			return $data;
		else
			echo $data;
	}

	// 다운목록 상품인지 확인(미니도매몰에서 다운로드 처리시 체크) 반영(2021.02.02)
	function down_plus_minus_goods_check()
	{
		$goods_id = $this->input->post('goodsId');
		$user_id = $this->session->userdata('user_id');

		$rtn_down_goods_check = 'N';

		// 다운상품이 맞는지 확인
		$sql1 = " SELECT id FROM goods_down WHERE user_id='{$user_id}' AND goods_id='{$goods_id}' ";
		$query1 = $this->db->query($sql1);
		$goods_down_rows = $query1->row();
		// debug($goods_down_rows);exit;

		// 다운목록 상품이면
		if($query1->num_rows() > 0) $rtn_down_goods_check = 'Y';

		echo '{"info":{"success":true, "down_goods_check":"'.$rtn_down_goods_check.'"}}';
	}

	function upload()
	{
		$data = $this->input->post();
		debug($data);
	}

	function product_new_kakao_msg_send() {
		$user_id = isset($_REQUEST['no']) ? $_REQUEST['no']:'';

		if($user_id == '' || $user_id != $this->view_data['user_id']) {
			alert("잘못된 접근 방식입니다.");
			exit;
		}

		$this->load->model('goods_m');
		$result = $this->goods_m->get_product_new_list($this->view_data['shop']['user_id']);
		
		$msg = "
안녕하세요~! 고객님~🧡
여성의류도매【#{도매회원}】에서 신상품 리스트를 보내드립니다.		
#{도매브랜드몰url}
📢사이트에 더많은 상품들이 준비되어있습니다.
보시고 샘플 요청 및 판매 진행 해보세요~!

👚신상품 리스트👚
🌈#{상품명1}
#{상품url1}
🌈#{상품명2}
#{상품url2}
🌈#{상품명3}
#{상품url3}
🧡사이즈UP, 최소수량 제작오더 가능
☎주문/샘플요청 #{도매전화번호}
✔카톡 바로 문의 가능합니다. 문의시 💨빠른 답변드립니다.
오늘도 좋은하루 되세요~^^!!💖";

		$msg = str_replace('#{도매회원}', $this->view_data['shop']['shop_name'], $msg);
		$msg = str_replace('#{도매브랜드몰url}', 'https://'.$this->view_data['userid'].'.newtalk.kr', $msg);
		$msg = str_replace('#{도매전화번호}', $this->view_data['shop']['shop_staff_hp'], $msg);

		if($result) {
			foreach($result as $key => $value) {
				$msg = str_replace('#{상품명'.(intval($key) + 1).'}', $value['GoodsEtc5'].'['.number_format($value['GoodsEtc9']).'원]', $msg);
				$msg = str_replace('#{상품url'.(intval($key) + 1).'}', 'https://'.$this->view_data['userid'].'.newtalk.kr/goods/detail/'.$value['id'], $msg);
			}
		}

		$message['msg'] = $msg;
		$json_msg = json_encode($message, JSON_UNESCAPED_UNICODE);

		echo '{"info":{"success":"true","text":"성공","data":'.$json_msg.'}}';
	}

	// 상품코드 이미지 압축다운
	// JSZip용 이미지 URL 목록 반환
	function goods_zip_urls()
	{
		if($this->view_data['down_level'] < 1) {
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(['success' => false, 'msg' => '다운로드 권한이 없습니다.']);
			return;
		}

		$goods_code = $this->input->get('code');
		if(!$goods_code) {
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(['success' => false, 'msg' => '잘못된 접근입니다.']);
			return;
		}

		$img_dir  = $this->config->item('user_goodscode_img_dir') . $goods_code . '/';
		$cdn_base = 'https://img.newtalk.kr/data/files/goods/goodscode/img/' . $goods_code . '/';

			$names = $this->_goodscode_image_files($goods_code);
			$urls  = [];
			foreach ($names as $name) {
				$urls[] = [
					'url' => $cdn_base . rawurlencode($name),
					'fallback_url' => '/goods/goods_zip_file?code=' . rawurlencode($goods_code) . '&file=' . rawurlencode($name),
					'zip_path' => $goods_code . '/' . $name
				];
			}

		$sql = "SELECT GS.*, GSD.* FROM goods AS GS
				LEFT OUTER JOIN goods_detail AS GSD ON GS.id = GSD.goods_id
				WHERE GS.GoodsCode='{$goods_code}'";
		$goods_info = $this->db->query($sql)->row();
		$txt = '';
		$goods_name = $goods_code;
		if ($goods_info && $goods_info->GoodsName) {
			if ($goods_info->GoodsEtc5) $goods_info->GoodsName = $goods_info->GoodsEtc5;
			$goods_name = $goods_info->GoodsName;
			$txt .= '[ ' . $goods_info->GoodsName . ' ]' . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= $goods_info->Description . PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= 'Product Info_' . PHP_EOL.PHP_EOL.PHP_EOL;
			$txt .= ' - FABRIC(소재) : ' . $goods_info->GoodsEtc16 . PHP_EOL.PHP_EOL;
			$txt .= ' - COLOR(색상) : ' . $goods_info->OptionColor . PHP_EOL.PHP_EOL;
			$txt .= ' - WASHING(세탁방법) : ' . $goods_info->GoodsEtc18 . PHP_EOL.PHP_EOL;
			$txt .= ' - Weight(무게:g) : ' . $goods_info->GoodsEtc17 . PHP_EOL.PHP_EOL;
			$txt .= ' - SIZE(사이즈) : ' . $goods_info->OptionSize . PHP_EOL.PHP_EOL;
			$txt .= ' - SizeSpec(상세사이즈) : ' . $goods_info->GoodsEtc14 . PHP_EOL.PHP_EOL;
		}

		header('Content-Type: application/json; charset=utf-8');
		echo json_encode([
			'success'    => true,
			'goods_code' => $goods_code,
				'goods_name' => $goods_name,
				'zip_name'   => urlencode($goods_name) . '.zip',
				'partial_zip_name' => urlencode($goods_name) . '_partial_missing.zip',
				'expected_count' => count($urls),
				'txt'        => $txt,
				'txt_name'   => $goods_code . '.txt',
				'images'     => $urls,
			], JSON_UNESCAPED_UNICODE);
		}

		function goods_zip_download_log()
		{
			$goods_code = $this->input->post('code');
			$expected = intval($this->input->post('expected'));
			$success = intval($this->input->post('success'));
			$failed = $this->input->post('failed');
			$user_id = $this->session->userdata('user_id') ? $this->session->userdata('user_id') : '0';
			$log = array(
				'type' => 'jszip_partial_download',
				'user_id' => $user_id,
				'goods_code' => $goods_code,
				'expected' => $expected,
				'success' => $success,
				'failed' => json_decode($failed, true),
				'ip' => $this->input->ip_address(),
				'ua' => substr((string)$this->input->user_agent(), 0, 200),
			);
			log_message('error', '[goods_zip_download] ' . json_encode($log, JSON_UNESCAPED_UNICODE));
			header('Content-Type: application/json; charset=utf-8');
			echo json_encode(array('success' => true));
		}

		function goods_zip_file()
		{
			if($this->view_data['down_level'] < 1) {
				show_404();
				return;
			}

			$goods_code = preg_replace('/[^a-zA-Z0-9_-]/', '', (string)$this->input->get('code'));
			$file = basename((string)$this->input->get('file'));
			if(!$goods_code || !$file || !in_array($file, $this->_goodscode_image_files($goods_code))) {
				show_404();
				return;
			}

			$file_path = $this->config->item('user_goodscode_img_dir') . $goods_code . '/' . $file;
			if(!is_file($file_path)) {
				show_404();
				return;
			}

			$mime = function_exists('mime_content_type') ? mime_content_type($file_path) : 'application/octet-stream';
			header('Content-Type: ' . ($mime ? $mime : 'application/octet-stream'));
			header('Content-Length: ' . filesize($file_path));
			header('Cache-Control: private, no-store, max-age=0');
			header('X-NewTalk-Download-Fallback: 1');
			readfile($file_path);
		}

	function goods_code_zip_down()
	{
		ini_set('memory_limit','-1'); // 메모리 무제한으로 풀기

		$this->load->model('goods_m');

		if($this->view_data['down_level'] < 1)
			alert('다운로드 권한이 없습니다!');

		$goods_id = $this->input->get('id');
		$goods_code = $this->input->get('code');
		$gb = $this->input->get('gb');

		if(!$goods_id && !$goods_code)
			alert_close('정상적인 접근이 아닙니다!');

		$this->load->library('zip'); // Zip 압축 클래스 초기화
		$this->load->library('excel');

		$excel_header_data = $this->config->item('excel_header_data');
		$excel_data = $this->config->item('excel_data');
		$excel_width_data = $this->config->item('excel_width_data');
		//debug($excel_width_data);

		// $goods_code = $this->uri->segment(3);

			$goods_img_dir = $this->config->item('user_goodscode_img_dir').$goods_code.'/';
			// 등록 상품 정보 확인
			$sql = "SELECT
						GS.id AS goods_pk,
						GS.*,
						GSD.*,
						GSI.*
					FROM
						goods AS		GS LEFT OUTER JOIN
						goods_detail	As GSD ON GS.id = GSD.goods_id LEFT OUTER JOIN
						goods_image		As GSI ON GS.id = GSI.goods_id
					WHERE GS.GoodsCode='{$goods_code}'
			";
			$query = $this->db->query($sql);
			$goods_info = $query->row();
			if(!$goods_info)
				alert_close('해당 상품 정보가 존재하지 않습니다!');
			$download_goods_id = (isset($goods_info->goods_pk) && $goods_info->goods_pk) ? $goods_info->goods_pk : $goods_id;
			//debug($sql);

		// 상품분류 (대>중>소>세)
		$goods_cate = '';
		$goods_cate1 = $goods_info->Category1;
		$goods_cate2_arr = explode('|', $goods_info->Category2);
		$goods_cate3_arr = explode('|', $goods_info->Category3);
		if($goods_cate1)
		{
			$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate1}");
			if($query->num_rows() > 0) $cate1 = $query->row();

			if($cate1->LargeCode)
			{
				$goods_cate .= $cate1->LargeCode;

				if($goods_cate2_arr[0])
				{
					$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate2_arr[0]}");
					if($query->num_rows() > 0) $cate2 = $query->row();

					if($cate2->MiddleCode)
					{
						$goods_cate .= '>'.$cate2->MiddleCode;

						if($goods_cate3_arr[0])
						{
							$query = $this->db->query("SELECT * FROM goods_cate WHERE id={$goods_cate3_arr[0]}");
							if($query->num_rows() > 0) $cate3 = $query->row();

							if($cate3->SmallCode)
								$goods_cate .= '>'.$cate3->SmallCode;
						}
					}
				}
			}
		}
		//debug($goods_cate);

		// 엑셀 자료 생성
		$real_excel_data = [];
		$headers = array();
		//debug(count($headers));

		// 엑셀 헤더
		foreach($excel_header_data AS $k => $v)
		{
			$headers[] = $v;
		}
		//debug($headers);

		// 엑셀 데이타
		$excel_data['A'] = $goods_info->GoodsName;	// 상품명[필수]
		$excel_data['B'] = $goods_info->GoodsEtc7;	// 약어
		$excel_data['C'] = $goods_info->GoodsEtc5;	// 모델명
		$excel_data['F'] = $goods_info->GoodsCode;	// 자체상품코드
		$excel_data['G'] = $goods_info->GoodsEtc24;	// 사이트검색어
		$excel_data['H'] = $goods_info->GoodsEtc48;	// 상품구분[필수]
		$excel_data['I'] = $goods_cate;					// 상품분류 (대>중>소>세)
		$excel_data['M'] = $goods_info->GoodsEtc21;	// 원산지(제조국)[필수]
		$excel_data['O'] = $goods_info->GoodsEtc20;	// 제조일
		$excel_data['P'] = $goods_info->GoodsEtc51;	// 시즌
		$excel_data['R'] = $goods_info->GoodsEtc52;	// 상품상태[필수]
		$excel_data['T'] = $goods_info->GoodsEtc53;	// 세금구분[필수]
		$excel_data['U'] = $goods_info->GoodsEtc54;	// 배송비구분[필수]
		$excel_data['X'] = $goods_info->GoodsEtc9;	// 원가
		$excel_data['Y'] = $goods_info->GoodsPrice;	// 판매가[필수]
		$excel_data['Z'] = $goods_info->GoodsEtc32;	// TAG가[필수]
		$excel_data['AB'] = $goods_info->OptionColor;	// 옵션상세명칭(1)
		$excel_data['AD'] = $goods_info->OptionSize;	// 옵션상세명칭(2)
		if($goods_info->GoodsImage)
		{
			$excel_data['AE'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->GoodsImage;	// 대표이미지[필수]
			$excel_data['AF'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->GoodsImage;	// 종합몰JPG이미지
		}
		if($goods_info->img1)
			$excel_data['AG'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img1;	// 부가이미지2
		if($goods_info->img2)
			$excel_data['AH'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img2;	// 부가이미지3
		if($goods_info->img3)
			$excel_data['AI'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img3;	// 부가이미지4
		if($goods_info->img4)
			$excel_data['AJ'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img4;	// 부가이미지5
		if($goods_info->img5)
			$excel_data['AK'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img5;	// 부가이미지6
		if($goods_info->img6)
			$excel_data['AL'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img6;	// 부가이미지7
		if($goods_info->img7)
			$excel_data['AM'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img7;	// 부가이미지8
		if($goods_info->img8)
			$excel_data['AN'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img8;	// 부가이미지9
		if($goods_info->img9)
			$excel_data['AO'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img9;	// 부가이미지10
		$excel_data['AP'] = $goods_info->Description;	// 상품상세설명[필수]
		$excel_data['AS'] = $goods_info->GoodsEtc56;	// 재고관리사용여부
		$excel_data['AT'] = $goods_info->GoodsEtc10;	// 원가2
		if($goods_info->img10)
			$excel_data['AU'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img10;	// 부가이미지11
		if($goods_info->img11)
			$excel_data['AV'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img11;	// 부가이미지12
		if($goods_info->img12)
			$excel_data['AY'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img12;	// 부가이미지14
		if($goods_info->img13)
			$excel_data['AZ'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img13;	// 부가이미지15
		if($goods_info->img14)
			$excel_data['BA'] = 'http://'.$_SERVER['HTTP_HOST'].'/data/files/goods/'.$goods_info->user_id.'/'.$goods_info->img14;	// 부가이미지16
		$excel_data['BJ'] = $goods_info->GoodsEtc4;	// 영문상품명
		$excel_data['BK'] = $goods_info->GoodsEtc8;	// 출력상품명
		$excel_data['BL'] = $goods_info->GoodsEtc27;	// 추가 상품상세설명_1(선택입력)
		$excel_data['BM'] = $goods_info->GoodsEtc28;	// 추가 상품상세설명_2(선택입력)
		$excel_data['BN'] = $goods_info->GoodsEtc29;	// 추가 상품상세설명_3(선택입력)
		$excel_data['BO'] = $goods_info->GoodsEtc57;	// 속성분류코드[필수]

		foreach($excel_data AS $k => $v)
		{
			$real_excel_data[] = $v;
		}
		//debug(count($real_excel_data));
		//debug($real_excel_data);exit;

		$rows = array(
			$real_excel_data,
		);
		//debug($rows);
		$data = array_merge(array($headers), $rows);
		//debug($data);

		// 스타일 지정
		$widths = array();
		foreach($excel_width_data AS $k => $v)
		{
			$widths[] = $v;
		}
		//debug(count($widths));
		$header_bgcolor = 'FFABCDEF';

		// 엑셀 생성
		$last_char = column_char( count($headers) - 1 );
		//debug($last_char);

		$excel = new PHPExcel();
		//$excel->setActiveSheetIndex(0)->getStyle( "A1:${last_char}1" )->getFill()->setFillType(PHPExcel_Style_Fill::FILL_SOLID)->getStartColor()->setARGB($header_bgcolor);
		//$excel->setActiveSheetIndex(0)->getStyle( "A:$last_char" )->getAlignment()->setVertical(PHPExcel_Style_Alignment::VERTICAL_CENTER)->setWrapText(true);
		foreach($widths as $i => $w) $excel->setActiveSheetIndex(0)->getColumnDimension( column_char($i) )->setWidth($w);
		//$excel->getActiveSheet()->fromArray($data, NULL, 'A1');
		//$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel2007');
		//$writer->save($this->config->item('user_temp_image_dir').$goods_code.'.xlsx');

		$col1 = 0;
		foreach ($headers as $title)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col1, 1, $title);
			$col1++;
		}
		$col2 = 0;
		foreach($real_excel_data as $record)
		{
			$excel->setActiveSheetIndex(0)->setCellValueByColumnAndRow($col2, 2, $record);
			$col2++;
		}

		// Add some data
		/*
		$excel->setActiveSheetIndex(0)
			->setCellValue("A1", $headers[0])->setCellValue("A2", $real_excel_data[0])
			->setCellValue("B1", $headers[1])->setCellValue("B2", $real_excel_data[1])
			->setCellValue("C1", $headers[2])->setCellValue("C2", $real_excel_data[2])
			->setCellValue("D1", $headers[3])->setCellValue("D2", $real_excel_data[3])
			->setCellValue("E1", $headers[4])->setCellValue("E2", $real_excel_data[4])
			->setCellValue("F1", $headers[5])->setCellValue("F2", $real_excel_data[5])
			->setCellValue("G1", $headers[6])->setCellValue("G2", $real_excel_data[6])
		;
		*/

		$excel->getActiveSheet()->setTitle('단하루 상품');
		$excel->setActiveSheetIndex(0);
		$writer = PHPExcel_IOFactory::createWriter($excel, 'Excel5');

		//$writer->save($this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_code.'.xls');
		//$this->zip->read_file($this->config->item('user_temp_image_dir').$this->view_data['user_id'].'/'.$goods_code.'.xls');

		$data_txt = '';
		if($goods_info->GoodsName)
		{
			// 공급회원은 상품명을 사입명으로 변경(2020.07.22)
			if($goods_info->GoodsEtc5) $goods_info->GoodsName = $goods_info->GoodsEtc5;

			$data_txt .= '[ '.$goods_info->GoodsName.' ]'.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ''.$goods_info->Description.PHP_EOL.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= 'Product Info_'.PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - FABRIC(소재) : '.$goods_info->GoodsEtc16.PHP_EOL.PHP_EOL;
			$data_txt .= ' - COLOR(색상) : '.$goods_info->OptionColor.PHP_EOL.PHP_EOL;
			$data_txt .= ' - 원단느낌 : '.$goods_info->GoodsEtc15.PHP_EOL.PHP_EOL;
			$data_txt .= ' - WASHING(세탁방법) : '.$goods_info->GoodsEtc18.PHP_EOL.PHP_EOL;
			$data_txt .= ' - Weight(무게:g) : '.$goods_info->GoodsEtc17.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SIZE(사이즈) : '.$goods_info->OptionSize.PHP_EOL.PHP_EOL;
			$data_txt .= ' - SizeSpec(상세사이즈) : '.$goods_info->GoodsEtc14.PHP_EOL.PHP_EOL;
			$data_txt .= PHP_EOL.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조사 : 단하루 협력업체'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조일 : 배송기준 3개월이내'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 제조국 : 대한민국'.PHP_EOL.PHP_EOL;
			$data_txt .= '- 품질보증기준 : 소비자보호 법에 준함'.PHP_EOL.PHP_EOL;
			$data_txt .= '※제품 문제 시 공정거래위원회에 고시된 소비자 분쟁해결 기준으로 보상합니다.'.PHP_EOL.PHP_EOL;

			$data = array(
                $goods_code.'.txt' => $data_txt
            );
			//debug($data);
			$this->zip->add_data($data);
		}

		$this->zip->read_dir($goods_img_dir, FALSE);

			$this->goods_m->down_status($this->view_data['user_id'], $download_goods_id, $goods_code);

	        if($gb == 'ajax')
	        {
				// 파일명 특수문자 제거, 2022.04.19
				$down_file = $this->_download_unique_zip_filename('pick_goods_img', $goods_info->GoodsEtc5 ? $goods_info->GoodsEtc5 : $goods_code);
				if($this->zip->archive($this->config->config['user_pick_image_dir'].$down_file))
	            {
				    echo json_encode(array('info' => array('success' => true, 'downfile' => $down_file)), JSON_UNESCAPED_UNICODE);
				    exit;
	            }
            else {
			    echo '{"info":{"success":false,"text":"다운로드 오류입니다."}}';
			    exit;
            }
        }
        else{
		    $this->zip->download(urlencode($goods_info->GoodsEtc5).'.zip'); // urlencode('단하루상품이미지텍스트파일').'('.date('YmdHis', time()).').zip'

        }

		// alert_close('다운로드가 완료되었습니다!');
		// alert('다운로드가 완료되었습니다!');
	}

	function test()
	{
		/*
		$arr = array(array('a'=>1),array('b'=>2),array('c'=>3));
		//$arr = array('a','b','c');
		var_dump($arr);
		echo "<BR><BR>";
		$d = shuffle($arr);
		var_dump($arr);
		*/

		$this->load->driver('cache');
		//$this->cache->file->save('foo', 'bar', 10);
		debug($this->cache->memcached);
		var_dump($this->cache->get('foo'));

		//$memcache = new Memcache;
		//$memcache->addServer('127.0.0.1', 11211);
		//@$memcache->connect('localhost', 11211) or die ("Could not connect");
		//$memcache->add('var_key', 'test variable', false, 30);
		//echo $memcache->getversion();
		//debug($memcache);

		//$mfilms = $memcache->get($memcache);

		//$version = $memcache->getVersion();
		//echo "Server's version: ".$version."<br/>\n";

		/*
		$host='localhost';
		$port=11211;
		$memcache = new Memcache();
		$memcache->addServer($host, $port);
		$stats = $memcache->getExtendedStats();
		$available = (bool) $stats["$host:$port"];
		if ($available && $memcache->connect($host, $port)){
			$this->connect=$memcache;
			echo 'true';
		}

		else{
				$host='localhost';
				$memcache->addServer($host, $port);
				$this->connect=$memcache;
				echo 'false';
		}
		*/
	}

	
// 상품 선택 담기
	function plus_minus_select()
	{
		$user_id = $this->session->userdata('user_id');
		$gb = $this->input->post('gb');
		$goodsId = $this->input->post('goodsId');
		//debug($gb);exit;

		if(!$goodsId)
		{
			echo '{"info":{"success":false,"text":"선택 된 상품이 없습니다."}}';
			exit;
		}

		if(!$user_id)
		{
			echo '{"info":{"success":false,"text":"로그인 후 이용 하실 수 있습니다."}}';
			exit;
		}

			if($goodsId > 0)
			{
				// 찜상품이 맞는지 확인
				$sql1 = " SELECT id, wishing FROM goods_wish WHERE user_id='{$user_id}' AND goods_id='{$goodsId}' ";
				$query1 = $this->db->query($sql1);
				$goods_wish_rows = $query1->row();

				// 아니면 등록
				if($query1->num_rows() < 1)
				{
					$wishing = 'Y';
					$insert_data = array(
						'user_id'	=> $user_id,
						'goods_id'	=> $goodsId,
						'wishing'	=> $wishing,
						'wish_ip'	=> $this->input->ip_address(),
						'created'	=> date('Y-m-d H:i:s')
					);
					$this->db->insert('goods_wish', $insert_data);

					$success_cnt++; // 성공 시 카운트
				}
				// 있으면 업데이트
				else
				{
					$wishing = ($goods_wish_rows->wishing == "Y") ? "N" : "Y";

					$update_data = array(
						'wishing' => $wishing,
						'wish_ip' => $this->input->ip_address()
					);
					//debug($update_data);
					$this->db->where('id', $goods_wish_rows->id);
					$this->db->update('goods_wish', $update_data);

					$success_cnt++; // 성공 시 카운트
				}
			}
//		}
		echo '{"info":{"success":true,"text":"선택상품 ['.$setting_cnt.' 개] 중 ['.$success_cnt.' 개] 상품이 처리되었습니다.", "wishing":"'. $wishing .'"}}';
	}

}

/* End of file goods.php */
/* Location: ./application/controllers/goods.php */
